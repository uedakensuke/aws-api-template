import json

from common.db_util import (
    DocumentDbAccess,
    ErrorCode,
    WebApiException,
    convert_array_of_dict_to_json_serializable,
)


def lambda_handler(event, context):
    try:
        # this is a sample.
        # - just read documents that have specified version from specified collection
        # - convert the documents to json, and return it as HTTP Response Body

        # ------------- SETTINGS -------------
        collectionName = "HelloWorldCollection"
        version = "HelloWorldVersion"
        limit = 100

        # ------------- BEGIN SAMPLE -------------
        collection = DocumentDbAccess().get_collection(
            collectionName,
        )
        documents = collection.find(
            filter={"version": version},
            projection={"_id": 0},
        ).limit(limit)

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "collection": collectionName,
                    "version": version,
                    "find_result": convert_array_of_dict_to_json_serializable(
                        documents
                    ),
                    "debug_info": {
                        "request pathParameters": event["pathParameters"]
                        if "pathParameters" in event
                        else {},
                        "request queryStringParameters": event["queryStringParameters"]
                        if "queryStringParameters" in event
                        else {},
                        "request body": event["body"] if "body" in event else {},
                    },
                }
            ),
        }
    except WebApiException as ex:
        return ex.create_error_response()
    except Exception as ex:
        webex = WebApiException(500, ErrorCode.ServerLogicError, str(ex))
        return webex.create_error_response()
