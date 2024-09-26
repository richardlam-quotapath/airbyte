#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import logging
from typing import TYPE_CHECKING, Any, List, Mapping, Tuple, Iterator

from airbyte_cdk.models import AirbyteMessage, ConfiguredAirbyteStream
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.connector_state_manager import ConnectorStateManager
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.utils.schema_helpers import InternalConfig
from requests import codes, exceptions  # type: ignore[import]
from airbyte_cdk.models import (
    AirbyteCatalog,
    AirbyteConnectionStatus,
    AirbyteMessage,
    AirbyteStateMessage,
    AirbyteStreamStatus,
    ConfiguredAirbyteCatalog,
    ConfiguredAirbyteStream,
    FailureType,
    Status,
    StreamDescriptor,
    SyncMode,
)
from airbyte_cdk.utils.stream_status_utils import as_airbyte_message as stream_status_as_airbyte_message
from airbyte_cdk.sources.streams.http.http import HttpStream
from airbyte_cdk.models import Type as MessageType

from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream

from .api import ZohoAPI
from .streams import ZohoStreamFactory, ZohoUsersStream

if TYPE_CHECKING:
    # This is a workaround to avoid circular import in the future.
    # TYPE_CHECKING is False at runtime, but True when system performs type checking
    # See details here https://docs.python.org/3/library/typing.html#typing.TYPE_CHECKING
    from airbyte_cdk.sources.streams import Stream


class SourceZohoCrm(AbstractSource):
    def check_connection(self, logger: logging.Logger, config: Mapping[str, Any]) -> Tuple[bool, any]:
        api = ZohoAPI(config)
        return api.check_connection()

    def _read_stream(
            self,
            logger: logging.Logger,
            stream_instance: Stream,
            configured_stream: ConfiguredAirbyteStream,
            state_manager: ConnectorStateManager,
            internal_config: InternalConfig,
        ) -> Iterator[AirbyteMessage]:
            if internal_config.page_size and isinstance(stream_instance, HttpStream):
                logger.info(f"Setting page size for {stream_instance.name} to {internal_config.page_size}")
                stream_instance.page_size = internal_config.page_size
            logger.debug(
                f"Syncing configured stream: {configured_stream.stream.name}",
                extra={
                    "sync_mode": configured_stream.sync_mode,
                    "primary_key": configured_stream.primary_key,
                    "cursor_field": configured_stream.cursor_field,
                },
            )
            stream_instance.log_stream_sync_configuration()

            stream_name = configured_stream.stream.name
            # The platform always passes stream state regardless of sync mode. We shouldn't need to consider this case within the
            # connector, but right now we need to prevent accidental usage of the previous stream state

            if stream_name == "users":
                stream_state = {}

            stream_state = (
                state_manager.get_stream_state(stream_name, stream_instance.namespace)
                if configured_stream.sync_mode == SyncMode.incremental
                else {}
            )

            if stream_state and "state" in dir(stream_instance) and not self._stream_state_is_full_refresh(stream_state):
                stream_instance.state = stream_state  # type: ignore # we check that state in the dir(stream_instance)
                logger.info(f"Setting state of {self.name} stream to {stream_state}")

            record_iterator = stream_instance.read(
                configured_stream,
                logger,
                self._slice_logger,
                stream_state,
                state_manager,
                internal_config,
            )

            record_counter = 0
            logger.info(f"Syncing stream: {stream_name} ")
            for record_data_or_message in record_iterator:
                record = self._get_message(record_data_or_message, stream_instance)
                if record.type == MessageType.RECORD:
                    record_counter += 1
                    if record_counter == 1:
                        logger.info(f"Marking stream {stream_name} as RUNNING")
                        # If we just read the first record of the stream, emit the transition to the RUNNING state
                        yield stream_status_as_airbyte_message(configured_stream.stream, AirbyteStreamStatus.RUNNING)
                yield from self._emit_queued_messages()
                yield record

            logger.info(f"Read {record_counter} records from {stream_name} stream")

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        module_streams = ZohoStreamFactory(config).produce()
        user_stream = [ZohoUsersStream(config)]
        module_streams.extend(user_stream)
        return module_streams
