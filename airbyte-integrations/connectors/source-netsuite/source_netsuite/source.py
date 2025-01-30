#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#


import logging
from collections import Counter
from json import JSONDecodeError
from typing import Any, List, Mapping, Tuple, Union

import requests
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from requests_oauthlib import OAuth1
from source_netsuite.constraints import CUSTOM_INCREMENTAL_CURSOR, INCREMENTAL_CURSOR, META_PATH, RECORD_PATH, SCHEMA_HEADERS
from source_netsuite.streams import (
    CustomIncrementalNetsuiteStream,
    IncrementalNetsuiteStream,
    LineItemsIncrementalNetsuiteStream,
    NetsuiteStream,
)


class SourceNetsuite(AbstractSource):

    logger: logging.Logger = logging.getLogger("airbyte")

    def auth(self, config: Mapping[str, Any]) -> OAuth1:
        # the `realm` param should be in format of: 12345_SB1
        realm = config["realm"].replace("-", "_").upper()
        return OAuth1(
            client_key=config["consumer_key"],
            client_secret=config["consumer_secret"],
            resource_owner_key=config["token_key"],
            resource_owner_secret=config["token_secret"],
            realm=realm,
            signature_method="HMAC-SHA256",
        )

    def base_url(self, config: Mapping[str, Any]) -> str:
        # the subdomain should be in format of: 12345-sb1
        subdomain = config["realm"].replace("_", "-").lower()
        return f"https://{subdomain}.suitetalk.api.netsuite.com"

    def get_session(self, auth: OAuth1) -> requests.Session:
        session = requests.Session()
        session.auth = auth
        return session

    def check_connection(self, logger, config: Mapping[str, Any]) -> Tuple[bool, Any]:
        auth = self.auth(config)
        object_types = config.get("object_types")
        base_url = self.base_url(config)
        session = self.get_session(auth)
        # if record types are specified make sure they are valid
        if object_types:
            # ensure there are no duplicate record types as this will break Airbyte
            duplicates = [k for k, v in Counter(object_types).items() if v > 1]
            if duplicates:
                return False, f'Duplicate record type: {", ".join(duplicates)}'
            # check connectivity to all provided `object_types`
            errors = []
            for object in object_types:
                # Line items will use the parent object's data
                object_name = object.lower().removesuffix("_line_items")
                
                try:
                    response = session.get(url=base_url + RECORD_PATH + object_name, params={"limit": 1})
                    response.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    errors.append(f"Error checking connection for object {object}: {e}")
            
            if errors:
                return False, "\n".join(errors)
            return True, None
        else:
            # if `object_types` are not provided, use `Contact` object
            # there should be at least 1 contact available in every NetSuite account by default.
            url = base_url + RECORD_PATH + "contact"
            try:
                response = session.get(url=url, params={"limit": 1})
                response.raise_for_status()
                return True, None
            except requests.exceptions.HTTPError as e:
                return False, e

    def get_schemas(self, object_names: Union[List[str], str], session: requests.Session, metadata_url: str) -> Mapping[str, Any]:
        """
        Handles multivariance of object_names type input and fetches the schema for each object type provided.
        """
        try:
            if isinstance(object_names, list):
                schemas = {}
                for object_name in object_names:
                    if object_name.endswith("_line_items"):
                        schemas.update(
                            **self.fetch_schema(object_name.removesuffix("_line_items"), session, metadata_url, override_name=object_name)
                        )
                        continue
                    schemas.update(**self.fetch_schema(object_name, session, metadata_url))
                return schemas
            elif isinstance(object_names, str):
                return self.fetch_schema(object_names, session, metadata_url)
            else:
                raise NotImplementedError(
                    f"Object Types has unknown structure, should be either `dict` or `str`, actual input: {object_names}"
                )
        except JSONDecodeError as e:
            self.logger.error(f"Unexpected output while fetching the object schema. Full error: {e.__repr__()}")

    def fetch_schema(self, object_name: str, session: requests.Session, metadata_url: str, override_name: str = "") -> Mapping[str, Any]:
        """
        Calls the API for specific object type and returns schema as a dict.
        """
        return {
            override_name if override_name else object_name.lower(): session.get(metadata_url + object_name, headers=SCHEMA_HEADERS).json()
        }

    def generate_stream(
        self,
        session: requests.Session,
        metadata_url: str,
        schemas: dict,
        object_name: str,
        auth: OAuth1,
        base_url: str,
        start_datetime: str,
        end_datetime: str,
        created_datetime: str,
        window_in_days: int,
        retry_concurrency_limit: bool,
        limit: int,
        max_retry: int = 3,
    ) -> Union[NetsuiteStream, IncrementalNetsuiteStream, CustomIncrementalNetsuiteStream]:

        input_args = {
            "auth": auth,
            "object_name": object_name,
            "base_url": base_url,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "created_datetime": created_datetime,
            "window_in_days": window_in_days,
            "retry_concurrency_limit": retry_concurrency_limit,
            "limit": limit,
        }

        schema = schemas[object_name]
        schema_props = schema.get("properties")

        is_line_item = object_name.endswith("_line_items")

        if schema_props:
            # Unblocks Alvaria for now (ch61340)
            if object_name == "employee":
                logging.info("Initializing Full Refresh Stream for Employee object")
                return NetsuiteStream(**input_args)
            elif is_line_item:
                line_item_name = object_name
                input_args["object_name"] = object_name.removesuffix("_line_items")
                return LineItemsIncrementalNetsuiteStream(**input_args, line_item_name=line_item_name)
            elif INCREMENTAL_CURSOR in schema_props.keys():
                logging.info(f"{INCREMENTAL_CURSOR} found in schema. Initializing Incremental Stream for {object_name}")
                return IncrementalNetsuiteStream(**input_args)
            elif CUSTOM_INCREMENTAL_CURSOR in schema_props.keys():
                logging.info(f"{CUSTOM_INCREMENTAL_CURSOR} found in schema. Initializing Custom Incremental Stream for {object_name}")
                return CustomIncrementalNetsuiteStream(**input_args)
            else:
                logging.info(f"No cursor field was found. Initializing Full Refresh Stream for {object_name}")
                # all other streams are full_refresh
                return NetsuiteStream(**input_args)
        else:
            retry_attempt = 1
            while retry_attempt <= max_retry:
                self.logger.warn(f"Object `{object_name}` schema has missing `properties` key. Retry attempt: {retry_attempt}/{max_retry}")
                # somethimes object metadata returns data with missing `properties` key,
                # we should try to fetch metadata again to that object
                schemas = self.get_schemas(object_name, session, metadata_url)
                if schemas[object_name].get("properties"):
                    input_args.update(**{"session": session, "metadata_url": metadata_url, "schemas": schemas})
                    return self.generate_stream(**input_args)
                retry_attempt += 1
            self.logger.warn(f"Object `{object_name}` schema is not available. Skipping this stream.")
            return None

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        auth = self.auth(config)
        session = self.get_session(auth)
        base_url = self.base_url(config)
        metadata_url = base_url + META_PATH
        object_names = config.get("object_types")

        # If customer selects invoice, salesorder, or creditmemo, automatically append line item objects to list
        line_item_mapping = {"invoice": "invoice_line_items", "salesorder": "salesorder_line_items", "creditmemo": "creditmemo_line_items"}

        for base_object, line_item in line_item_mapping.items():
            if base_object in object_names and line_item not in object_names:
                object_names.append(line_item)

        # retrieve all record types if `object_types` config field is not specified
        if not object_names:
            objects_metadata = session.get(metadata_url).json().get("items")
            object_names = [object["name"] for object in objects_metadata]

        input_args = {"session": session, "metadata_url": metadata_url}
        schemas = self.get_schemas(object_names, **input_args)
        input_args.update(
            **{
                "auth": auth,
                "base_url": base_url,
                "start_datetime": config["start_datetime"],
                "end_datetime": config.get("end_datetime"),
                "created_datetime": config.get("created_datetime"),
                "window_in_days": config["window_in_days"],
                "retry_concurrency_limit": config.get("retry_concurrency_limit"),
                "limit": config.get("limit", 500),
                "schemas": schemas,
            }
        )
        # build streams
        streams: list = []
        for name in object_names:
            stream = self.generate_stream(object_name=name.lower(), **input_args)
            if stream:
                streams.append(stream)
        return streams
