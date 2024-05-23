#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import concurrent.futures
import datetime
import logging
import math
from abc import ABC
from dataclasses import asdict
from http import HTTPStatus
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional

import requests
from airbyte_cdk.sources.streams.http import HttpStream
from airbyte_cdk.models import SyncMode

from .api import ZohoAPI
from .exceptions import IncompleteMetaDataException, UnknownDataTypeException
from .types import FieldMeta, ModuleMeta, ZohoPickListItem
from airbyte_cdk.sources.streams.core import StreamData

# 204 and 304 status codes are valid successful responses,
# but `.json()` will fail because the response body is empty
EMPTY_BODY_STATUSES = (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED)

logger = logging.getLogger("airbyte")


class ZohoCrmStream(HttpStream, ABC):
    primary_key: str = "id"
    module: ModuleMeta = None
    FIELDS_PER_QUERY = 50

    def __init__(self, authenticator: "requests.auth.AuthBase" = None):
        super().__init__(authenticator)
        # Zoho's API limits us to querying for 50 fields max at a time, so we'll use a fields_cursor to page all the fields
        self.fields_cursor = 0

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        if response.status_code in EMPTY_BODY_STATUSES:
            return None
        pagination = response.json()["info"]
        if not pagination["more_records"]:
            return None
        next_page_token = {"page_token": pagination["next_page_token"]}
        logger.info(f"Next Page Token {next_page_token}")
        return next_page_token

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        return next_page_token or {}

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any],
        stream_slice: Optional[Mapping[str, Any]] = None,
        # This is never going to be the next_page_token without our modifications below to the read_records and _read_pages functions :/,
        # Based on the order that Airbyte has structured their calls, this function never has access to the next page token
        # because the records_generator_fn only sets the first 3 args, so next_page_token is always null despite what the function definition has you believe.
        # Only for this zoho source connector will the next_page_token be set since we're modifying the read_records and _read_pages calls to include it.
        next_page_token: Optional[Mapping[str, Any]] = None,
    ) -> Iterable[Mapping]:
        main_data = [] if response.status_code in EMPTY_BODY_STATUSES else response.json()["data"]
        self.fields_cursor = 0

        total_fields = len(self.module.fields)
        # Zoho's API limits us to querying for 50 fields max at a time. We should check if we have queried for all the fields or if we need to send
        # sub-queries to get the additional fields.
        if self.fields_cursor + self.FIELDS_PER_QUERY >= total_fields:
            self.logger.info(f"All {total_fields} fields have been retrieved.")
            yield from main_data
        else:
            self.logger.info(f"Additional fields need to be retrieved. Total number of fields: {total_fields}")

            fields_data_lst: list[list[dict]] = []
            while self.fields_cursor + self.FIELDS_PER_QUERY < total_fields:
                self.fields_cursor += self.FIELDS_PER_QUERY

                # We're using _fetch_next_page but with the fields_cursor incremented so the method will return the next set of fields
                _, response = self._fetch_next_page(stream_slice=stream_slice, stream_state=stream_state, next_page_token=next_page_token)
                fields_data = [] if response.status_code in EMPTY_BODY_STATUSES else response.json()["data"]
                fields_data_lst.append(fields_data)

            # Join all the data from the subqueries here and return one list containing records with all their fields
            final_joined_data = []
            for i in range(len(main_data)):
                main_record = main_data[i]

                # Combine all the fields from the subqueries into one object additional_fields_data
                additional_fields_data = {}
                for fields_data in fields_data_lst:
                    main_record_additional_fields = fields_data[i]
                    additional_fields_data = {**additional_fields_data, **main_record_additional_fields}

                # Combine additional_fields_data with the main record
                final_joined_data.append({**main_record, **additional_fields_data})

            yield from final_joined_data

    def path(self, *args, **kwargs) -> str:
        fields = ",".join(
            [field.api_name for field in self.module.fields][self.fields_cursor : self.fields_cursor + self.FIELDS_PER_QUERY]
        )  # Note, limited to 50 fields at max (https://www.zoho.com/crm/developer/docs/api/v4/get-records.html)
        self.logger.info(f"Sending request for fields: {fields}")
        return f"/crm/v4/{self.module.api_name}?fields={fields}"

    def get_json_schema(self) -> Optional[Dict[Any, Any]]:
        try:
            return asdict(self.module.schema)
        except IncompleteMetaDataException:
            # to build a schema for a stream, a sequence of requests is made:
            # one `/settings/modules` which introduces a list of modules,
            # one `/settings/modules/{module_name}` per module and
            # one `/settings/fields?module={module_name}` per module.
            # Any of former two can result in 204 and empty body what blocks us
            # from generating stream schema and, therefore, a stream.
            self.logger.warning(
                f"Could not retrieve fields Metadata for module {self.module.api_name}. " f"This stream will not be available for syncs."
            )
            return None
        except UnknownDataTypeException as exc:
            self.logger.warning(f"Unknown data type in module {self.module.api_name}, skipping. Details: {exc}")
            raise

    def _read_pages(
        self,
        records_generator_fn: Callable[
            [requests.PreparedRequest, requests.Response, Mapping[str, Any], Optional[Mapping[str, Any]]], Iterable[StreamData]
        ],
        stream_slice: Optional[Mapping[str, Any]] = None,
        stream_state: Optional[Mapping[str, Any]] = None,
    ) -> Iterable[StreamData]:
        stream_state = stream_state or {}
        pagination_complete = False
        next_page_token = None
        while not pagination_complete:
            # Reset fields_cursor right before we fetch the next page of records
            self.fields_cursor = 0
            request, response = self._fetch_next_page(stream_slice, stream_state, next_page_token)

            # IMPORTANT: Add next_page_token here
            yield from records_generator_fn(request, response, stream_state, stream_slice, next_page_token)

            next_page_token = self.next_page_token(response)
            if not next_page_token:
                pagination_complete = True

        # Always return an empty generator just in case no records were ever yielded
        yield from []

    def read_records(
        self,
        sync_mode: SyncMode,
        cursor_field: Optional[List[str]] = None,
        stream_slice: Optional[Mapping[str, Any]] = None,
        stream_state: Optional[Mapping[str, Any]] = None,
    ) -> Iterable[StreamData]:
        yield from self._read_pages(
            # Important: Add next_page_token to lambda function
            lambda req, res, state, slice, next_page_token: self.parse_response(
                res, stream_slice=slice, stream_state=state, next_page_token=next_page_token
            ),
            stream_slice,
            stream_state,
        )


class IncrementalZohoCrmStream(ZohoCrmStream):
    cursor_field = "Modified_Time"

    def __init__(self, authenticator: "requests.auth.AuthBase" = None, config: Mapping[str, Any] = None):
        super().__init__(authenticator)
        self._config = config
        self._state = {}
        self._start_datetime = self._config.get("start_datetime") or "1970-01-01T00:00:00+00:00"

    @property
    def state(self) -> Mapping[str, Any]:
        if not self._state:
            self._state = {self.cursor_field: self._start_datetime}
        return self._state

    @state.setter
    def state(self, value: Mapping[str, Any]):
        self._state = value

    def read_records(self, *args, **kwargs) -> Iterable[Mapping[str, Any]]:
        """
        4/17/2024 - Modified so its dynamically full refresh or incremental. If the cursor field is present then we will compare it to the
        current state's cursor, otherwise we will always yield it if the cursor field is not present.
        """
        logger.info(f"Beginning read_records for stream {self.name}.")
        records = super().read_records(*args, **kwargs)

        current_cursor_value = datetime.datetime.fromisoformat(self.state[self.cursor_field])
        new_cursor_value = current_cursor_value

        for record in records:
            try:
                # This is going to be a large output, but worth it for debugging
                logger.info(f"Found record {record}")

                if self.cursor_field not in record:
                    logger.info(f"Cursor field {self.cursor_field} not found in record. Yielding record.")
                    yield record
                    continue

                if (latest_cursor_value := datetime.datetime.fromisoformat(record[self.cursor_field])) <= current_cursor_value:
                    logger.info(f"Record is less than current cursor value {current_cursor_value}. Skipping record.")
                    continue

                new_cursor_value = max(latest_cursor_value, new_cursor_value)
                yield record
            except Exception as e:
                logger.warning("The error below occurred while reading a record. Skipping record.")
                logger.warning(e)
                continue

        self.state = {self.cursor_field: new_cursor_value.isoformat("T", "seconds")}


class ZohoStreamFactory:
    def __init__(self, config: Mapping[str, Any]):
        self.api = ZohoAPI(config)
        self._config = config

    def _init_modules_meta(self) -> List[ModuleMeta]:
        modules_meta_json = self.api.modules_settings()
        modules = [ModuleMeta.from_dict(module) for module in modules_meta_json]
        return list(filter(lambda module: module.api_supported, modules))

    def _populate_fields_meta(self, module: ModuleMeta):
        fields_meta_json = self.api.fields_settings(module.api_name)
        fields_meta = []
        for field in fields_meta_json:
            pick_list_values = field.get("pick_list_values", [])
            if pick_list_values:
                field["pick_list_values"] = [ZohoPickListItem.from_dict(pick_list_item) for pick_list_item in field["pick_list_values"]]
            fields_meta.append(FieldMeta.from_dict(field))
        module.fields = fields_meta

    def _populate_module_meta(self, module: ModuleMeta):
        module_meta_json = self.api.module_settings(module.api_name)
        module.update_from_dict(next(iter(module_meta_json), None))

    def produce(self) -> List[HttpStream]:
        logger.info("Initializing modules metadata")
        modules = self._init_modules_meta()
        streams = []

        def populate_module(module):
            self._populate_module_meta(module)
            self._populate_fields_meta(module)

        def chunk(max_len, lst):
            for i in range(math.ceil(len(lst) / max_len)):
                yield lst[i * max_len : (i + 1) * max_len]

        max_concurrent_request = self.api.max_concurrent_requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent_request) as executor:
            for batch in chunk(max_concurrent_request, modules):
                executor.map(lambda module: populate_module(module), batch)

        bases = (IncrementalZohoCrmStream,)
        logger.info("Initializing streams")
        for module in modules:
            stream_cls_attrs = {"url_base": self.api.api_url, "module": module}
            stream_cls_name = f"Incremental{module.api_name}ZohoCRMStream"
            incremental_stream_cls = type(stream_cls_name, bases, stream_cls_attrs)
            stream = incremental_stream_cls(self.api.authenticator, config=self._config)
            if stream.get_json_schema():
                logger.info(f"JSON Schema found for stream {stream.name}. Adding to streams list.")
                streams.append(stream)
        return streams
