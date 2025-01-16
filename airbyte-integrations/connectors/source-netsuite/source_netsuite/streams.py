#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#


from abc import ABC
from datetime import date, datetime, timedelta
from time import sleep
from json import JSONDecodeError
import logging
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Tuple, Union

import requests
from airbyte_cdk.sources.streams.http import HttpStream
from requests_oauthlib import OAuth1
from airbyte_cdk.sources.streams.http.exceptions import DefaultBackoffException, UserDefinedBackoffException
from source_netsuite.constraints import (
    CREATED_DATETIME,
    CREATED_DATETIME_ALT,
    CUSTOM_DATE_FIELD,
    CUSTOM_INCREMENTAL_CURSOR,
    INCREMENTAL_CURSOR,
    META_PATH,
    NETSUITE_INPUT_DATE_FORMATS,
    NETSUITE_OUTPUT_DATETIME_FORMAT,
    OBJECTS_USING_ALT_DATETIME_FIELD,
    RECORD_PATH,
    REFERAL_SCHEMA,
    REFERAL_SCHEMA_URL,
    SCHEMA_HEADERS,
    USLESS_SCHEMA_ELEMENTS,
)
from source_netsuite.errors import NETSUITE_ERRORS_MAPPING, DateFormatExeption


class NetsuiteStream(HttpStream, ABC):
    def __init__(
        self,
        auth: OAuth1,
        object_name: str,
        base_url: str,
        start_datetime: str,
        end_datetime: str,
        created_datetime: str,
        window_in_days: int,
        retry_concurrency_limit: bool,
    ):
        self.object_name = object_name
        self.base_url = base_url
        self.start_datetime = start_datetime
        self.end_datetime = end_datetime or datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.created_datetime = (
            datetime.strptime(created_datetime, "%m/%d/%Y") if created_datetime else datetime.now() - timedelta(days=365)
        )
        self.window_in_days = window_in_days
        self.retry_concurrency_limit = retry_concurrency_limit or False
        self.schemas = {}  # store subschemas to reduce API calls
        super().__init__(authenticator=auth)

    primary_key = "id"

    # instance input date format format selector
    index_datetime_format = 0

    raise_on_http_errors = True

    @property
    def default_datetime_format(self) -> str:
        return NETSUITE_INPUT_DATE_FORMATS[self.index_datetime_format]

    @property
    def name(self) -> str:
        return self.object_name

    @property
    def url_base(self) -> str:
        return self.base_url

    def path(self, **kwargs) -> str:
        return RECORD_PATH + self.object_name

    @property
    def ref_schema(self) -> Mapping[str, str]:
        schema = REFERAL_SCHEMA
        schema["properties"]["links"]["items"] = self.get_schema(REFERAL_SCHEMA_URL)
        return schema

    def get_schema(self, ref: str) -> Union[Mapping[str, Any], str]:
        def get_json_response(response: requests.Response) -> dict:
            try:
                return response.json()
            except JSONDecodeError as e:
                self.logger.error(f"Cannot get schema for {self.name}, actual response: {e.response.text}")
                raise

        # try to retrieve the schema from the cache
        schema = self.schemas.get(ref)
        if not schema:
            resp = self._session.get(url=self.url_base + ref, headers=SCHEMA_HEADERS)
            # some schemas, like transaction, do not exist because they refer to multiple
            # record types, e.g. sales order/invoice ... in this case we can't retrieve
            # the correct schema, so we just put the json in a string
            if resp.status_code == 404:
                schema = {"title": ref, "type": "string"}
            else:
                # check for 200 status
                resp.raise_for_status
                # handle response
                schema = get_json_response(resp)

            self.schemas[ref] = schema
        return schema

    def build_schema(self, record: Any) -> Mapping[str, Any]:
        # recursively build a schema with subschemas
        if isinstance(record, dict):
            # Netsuite schemas do not specify if fields can be null, or not
            # as Airbyte expects, so we have to allow every field to be null
            property_type = record.get("type")
            property_type_list = property_type if isinstance(property_type, list) else [property_type]
            # ensure there is a type, type is the json schema type and not a property
            # and null has not already been added
            if property_type and not isinstance(property_type, dict) and "null" not in property_type_list:
                record["type"] = ["null"] + property_type_list
            # removing non-functional elements from schema
            [record.pop(element) for element in USLESS_SCHEMA_ELEMENTS if record.get(element)]

            ref = record.get("$ref")
            if ref:
                return self.get_schema(ref) if ref == REFERAL_SCHEMA_URL else self.ref_schema
            else:
                return {k: self.build_schema(v) for k, v in record.items()}
        else:
            return record

    def get_json_schema(self, **kwargs) -> dict:
        schema = self.get_schema(META_PATH + self.name)
        return self.build_schema(schema)

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        resp = response.json()
        has_more = resp.get("hasMore")
        if has_more:
            return {"offset": resp["offset"] + resp["count"]}
        return None

    def format_date(self, last_modified_date: str) -> str:
        # the date format returned is differnet than what we need to use in the query
        lmd_datetime = datetime.strptime(last_modified_date, NETSUITE_OUTPUT_DATETIME_FORMAT)
        return lmd_datetime.strftime(self.default_datetime_format)

    def request_params(self, next_page_token: Mapping[str, Any] = None, **kwargs) -> MutableMapping[str, Any]:
        params = {**(next_page_token or {}), **{"limit": 500}}
        return params

    def fetch_record(self, record: Mapping[str, Any], request_kwargs: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        url = record["links"][0]["href"]
        args = {"method": "GET", "url": url, "params": {"expandSubResources": True}}
        prep_req = self._session.prepare_request(requests.Request(**args))
        response = self._send_request(prep_req, request_kwargs)

        self.logger.info(f"Status code {response.status_code} for request: {url}")
        # sometimes response.status_code == 400,
        # but contains json elements with error description,
        # to avoid passing it as {TYPE: RECORD}, we filter response by status
        if response.status_code == requests.codes.ok:
            yield response.json()

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
        **kwargs,
    ) -> Iterable[Mapping]:
        records = response.json().get("items", [])
        totalResults = response.json().get("totalResults")
        self.logger.info(f"Total records found for current query: {totalResults}")

        request_kwargs = self.request_kwargs(stream_slice, next_page_token)

        if records:
            errored_records = []
            for record in records:
                # make sub-requests for each record fetched
                try:
                    yield from self.fetch_record(record, request_kwargs)
                except Exception as e:
                    url = record.get("links", [{}])[0].get("href")
                    self.logger.info(f"Encountered the error below while fetching url {url}. Adding to error list.")
                    self.logger.info(e)
                    errored_records.append(record)
                    continue

            if errored_records:
                self.logger.info(f"BEGIN: Fetching {len(errored_records)} records in error list.")
                for record in errored_records:
                    try:
                        yield from self.fetch_record(record, request_kwargs)
                    except Exception as e:
                        self.logger.info(f"Encountered error again while fetching url {url}. Skipping record.")
                        continue

    def should_retry(self, response: requests.Response) -> bool:
        if response.status_code in NETSUITE_ERRORS_MAPPING.keys():
            self.logger.warning(f"Error occured, Response code {response.status_code}")

            message = response.json().get("o:errorDetails")
            self.logger.warning(f"Error Details: {message}")

            if isinstance(message, list):
                error_code = message[0].get("o:errorCode")
                self.logger.warning(f"NetSuite Error Code: {error_code}")
                detail_message = message[0].get("detail")
                known_error = NETSUITE_ERRORS_MAPPING.get(response.status_code)

                if error_code in known_error.keys():
                    setattr(self, "raise_on_http_errors", False)

                    if "INVALID_LOGIN" in error_code:
                        self.logger.warning(f"Invalid Login Error. Will attempt retry.")
                        return True

                    # Handle 429 CONCURRENCY_LIMIT_EXCEEDED by trying again after short break
                    # This approach was borrowed from another NS connector contributor (see https://github.com/airbytehq/airbyte/pull/35747)
                    if "CONCURRENCY_LIMIT_EXCEEDED" in error_code and self.retry_concurrency_limit is True:
                        self.logger.warning(f"Concurrency Limit Error. Will attempt retry after sleep.")
                        sleep(5)
                        return True

                    # handle data-format error
                    if "INVALID_PARAMETER" in error_code and "failed with date format" in detail_message:
                        self.logger.warning(f"Stream `{self.name}`: cannot read using date format `{self.default_datetime_format}")
                        self.index_datetime_format += 1
                        if self.index_datetime_format < len(NETSUITE_INPUT_DATE_FORMATS):
                            self.logger.warning(f"Stream `{self.name}`: retry using next date format `{self.default_datetime_format}")
                            raise DateFormatExeption
                        else:
                            self.logger.error(f"DATE FORMAT exception. Cannot read using known formats {NETSUITE_INPUT_DATE_FORMATS}")
                            return False

                    # handle other known errors
                    self.logger.error(f"Stream `{self.name}`: {error_code} error occured, full error message: {detail_message}")
                    return False
                else:
                    return super().should_retry(response)
        return super().should_retry(response)

    def read_records(
        self, stream_slice: Mapping[str, Any] = None, stream_state: Mapping[str, Any] = None, **kwargs
    ) -> Iterable[Mapping[str, Any]]:
        try:
            yield from super().read_records(stream_slice=stream_slice, stream_state=stream_state, **kwargs)
        except DateFormatExeption:
            """continue trying other formats, until the list is exhausted"""


class IncrementalNetsuiteStream(NetsuiteStream):
    @property
    def cursor_field(self) -> str:
        return INCREMENTAL_CURSOR

    @property
    def created_date_field(self) -> str:
        # Determine which created date field to use
        return CREATED_DATETIME_ALT if self.object_name in OBJECTS_USING_ALT_DATETIME_FIELD else CREATED_DATETIME

    def filter_records_newer_than_state(
        self,
        stream_state: Mapping[str, Any] = None,
        records: Mapping[str, Any] = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Parse the records with respect to `stream_state` for `incremental` sync."""
        if stream_state:
            for record in records:
                if record.get(self.cursor_field, self.start_datetime) >= stream_state.get(self.cursor_field):
                    yield record
        else:
            yield from records

    def get_state_value(self, stream_state: Mapping[str, Any] = None) -> str:
        """
        Sometimes the object has no `cursor_field` value assigned, and the ` "" ` emmited as state value,
        this causes conflicts with datetime lib to parse the `time component`,
        to avoid the errors we falling back to default start_date from input config.
        """
        state = stream_state.get(self.cursor_field) if stream_state else self.start_datetime
        if not state:
            self.logger.info(f"Stream state for `{self.name}` was not emmited, falling back to default value: {self.start_datetime}")
            return self.start_datetime
        return state

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
        **kwargs,
    ) -> Iterable[Mapping]:
        records = super().parse_response(response, stream_state, stream_slice, next_page_token)
        new_records = self.filter_records_newer_than_state(stream_state, records)
        for record in new_records:
            if self.name in ["invoice", "creditmemo", "salesorder"]:
                # Drop line items
                try:
                    record["item"]["items"] = []
                except KeyError:
                    yield record
            yield record
        return {}

    def get_updated_state(
        self,
        current_stream_state: MutableMapping[str, Any],
        latest_record: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        latest_cursor = latest_record.get(self.cursor_field, self.start_datetime)
        current_cursor = current_stream_state.get(self.cursor_field, self.start_datetime)
        return {self.cursor_field: max(latest_cursor, current_cursor)}

    def request_params(
        self, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None, **kwargs
    ) -> MutableMapping[str, Any]:
        # At most, our destination will be configured to write 500 records, so we don't need to fetch the entire 1000 default limit. This might help stabilize the syncs with less data per GET.
        params = {**(next_page_token or {}), **{"limit": 500}}
        # Format the created date query param to match the date format used in the records (determined through retry logic if initial format fails.
        formatted_created_datetime = self.created_datetime.strftime(self.default_datetime_format)

        # Update query based on stream slice
        if stream_slice:
            params.update(
                {
                    "q": f'{self.created_date_field} ON_OR_AFTER "{formatted_created_datetime}" AND {self.cursor_field} ON_OR_AFTER "{stream_slice["start"]}" AND {self.cursor_field} BEFORE "{stream_slice["end"]}"'
                }
            )

        self.logger.info(f"Request params -> {params}")
        return params

    def stream_slices(self, stream_state: Mapping[str, Any] = None, **kwargs) -> Iterable[Optional[Mapping[str, Any]]]:
        # Netsuite cannot order records returned by the API, so we need stream slices
        # to maintain state properly https://docs.airbyte.com/connector-development/cdk-python/incremental-stream/#streamstream_slices

        slices = []
        state = self.get_state_value(stream_state)
        start = datetime.strptime(state, NETSUITE_OUTPUT_DATETIME_FORMAT).date()
        end = datetime.strptime(self.end_datetime, NETSUITE_OUTPUT_DATETIME_FORMAT).date()

        # handle abnormal state values
        if start > date.today():
            return slices
        else:
            while start < end:
                next_day = start + timedelta(days=self.window_in_days)
                next_day = end if end < next_day else next_day

                # Validate the dates
                # NOTE: occasionally February is skipped over entirely with the slice, but it's difficult to repro and debug, so adding some validation and logging
                try:
                    datetime(next_day.year, next_day.month, next_day.day)
                except ValueError:
                    self.logger.info(f"Invalid date generated: {next_day}")
                    continue

                slice_start = start.strftime(self.default_datetime_format)
                slice_end = next_day.strftime(self.default_datetime_format)
                yield {"start": slice_start, "end": slice_end}
                start = next_day


class LineItemsIncrementalNetsuiteStream(IncrementalNetsuiteStream):
    def __init__(
        self,
        auth: OAuth1,
        object_name: str,
        base_url: str,
        start_datetime: str,
        end_datetime: str,
        created_datetime: str,
        window_in_days: int,
        retry_concurrency_limit: bool,
        line_item_name: str,
    ):
        self.line_item_name = line_item_name
        super().__init__(
            auth, object_name, base_url, start_datetime, end_datetime, created_datetime, window_in_days, retry_concurrency_limit
        )

    @property
    def name(self) -> str:
        # The base object we're using for all the data fetching is the object_name,
        # but the stream name should be the actual line item's name
        return self.line_item_name

    def get_json_schema(self, **kwargs) -> dict:
        schema = self.get_schema(META_PATH + self.object_name)

        raw_line_item_schema = schema["properties"]["item"]["properties"]["items"]["items"]
        json_schema = self.build_schema(raw_line_item_schema)

        # Make sure primary key & cursor field are present in schema
        json_schema["properties"]["id"] = {"type": "string"}
        json_schema["properties"]["lastModifiedDate"] = {"type": "string"}

        # Add the id of the parent record to the line item to join the line item back to the parent record
        json_schema["properties"][f"{self.object_name}_id"] = {"type": "string"}

        return json_schema

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
        **kwargs,
    ) -> Iterable[Mapping]:
        # Get all parent records (invoice, salesorder, or creditmemo)
        records = super().parse_response(response, stream_state, stream_slice, next_page_token)
        # Filter to get newest ones
        filtered_records = self.filter_records_newer_than_state(stream_state, records)

        # Return the line items from parent records
        for record in filtered_records:
            line_items = record.get("item", {}).get("items", [])
            for line_item_record in line_items:
                # Add primary key to line item record
                primary_key = f'{record["id"]}_{line_item_record["line"]}'
                line_item_record["id"] = primary_key

                # Add the id of the parent record to the line item to join the line item back to the parent record
                line_item_record[f"{self.object_name}_id"] = record["id"]

                # Add cursor field for incremental streams
                line_item_record[self.cursor_field] = record[self.cursor_field]

                yield line_item_record


class CustomIncrementalNetsuiteStream(IncrementalNetsuiteStream):
    @property
    def cursor_field(self) -> str:
        return CUSTOM_INCREMENTAL_CURSOR

    @property
    def created_date_field(self) -> str:
        return CUSTOM_DATE_FIELD
