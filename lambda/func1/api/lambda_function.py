import json

from common.sample_util import sample_func


def lambda_handler(event, context):
    try:
        return {
            "statusCode": 200,
            "body": json.dumps({"msg": sample_func()}),
        }
    except Exception as ex:
        return {"statusCode": 500, "body": json.dumps({"exception": str(ex)})}
