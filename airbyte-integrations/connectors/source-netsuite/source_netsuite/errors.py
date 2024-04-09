#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#


# Richard - Useful notes
# Raw 401 json
# {'type': 'https://www.rfc-editor.org/rfc/rfc9110.html#section-15.5.2', 'title': 'Unauthorized', 'status': 401, 'o:errorDetails': [{'detail': 'Invalid login attempt. For more details, see the Login Audit Trail in the NetSuite UI at Setup > Users/Roles > User Management > View Login Audit Trail.', 'o:errorCode': 'INVALID_LOGIN'}]}

# NETSUITE ERROR CODES BY THEIR HTTP TWINS
NETSUITE_ERRORS_MAPPING: dict = {
    400: {
        "USER_ERROR": "reading an Admin record allowed for Admin only",
        "NONEXISTENT_FIELD": "cursor_field declared in schema but doesn't exist in object",
        "INVALID_PARAMETER": "cannot read or find the object. Skipping",
    },
    401: {
        "INVALID_LOGIN" : "invalid login"
    },
    403: {
        "INSUFFICIENT_PERMISSION": "not enough permissions to access the object",
    },
}


# NETSUITE API ERRORS EXCEPTIONS
class DateFormatExeption(Exception):
    """API CANNOT HANDLE REQUEST USING GIVEN DATETIME FORMAT"""
