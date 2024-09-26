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
        print("SYNC MODE", configured_stream.sync_mode)
        print("STREAM STATE", state_manager.get_stream_state(configured_stream.stream.name, stream_instance.namespace))

        yield from super()._read_stream(logger, stream_instance, configured_stream, state_manager, internal_config)


    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        module_streams = ZohoStreamFactory(config).produce()
        user_stream = [ZohoUsersStream(config)]
        module_streams.extend(user_stream)
        return module_streams
