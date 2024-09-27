#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import logging
from typing import Any, List, Mapping, Tuple

from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream

from .api import ZohoAPI
from .streams import ZohoStreamFactory, ZohoUsersStream


class SourceZohoCrm(AbstractSource):
    def check_connection(self, logger: logging.Logger, config: Mapping[str, Any]) -> Tuple[bool, any]:
        api = ZohoAPI(config)
        return api.check_connection()

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        module_streams = ZohoStreamFactory(config).produce()
        user_stream = [ZohoUsersStream(config)]
        module_streams.extend(user_stream)
        return module_streams
