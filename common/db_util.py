from __future__ import annotations

import json
import math
import os
import re
import sys
import traceback
from datetime import datetime
from enum import Enum
from glob import glob
from typing import Any, Sequence

import boto3
from botocore.exceptions import ClientError
from bson.objectid import ObjectId
from bson.timestamp import Timestamp
from mypy_boto3_s3 import S3Client, S3ServiceResource  # from boto3-stubs
from pymongo.collection import Collection
from pymongo.cursor import Cursor
from pymongo.database import Database
from pymongo.mongo_client import MongoClient
from pymongo.results import UpdateResult

# エラー処理用のutil ----------------------------------------------------------------


class ErrorCode(Enum):
    ParamNotFound = 100
    BadFormatParam = 101
    CollectionNotExist = 200
    DocumentNotExist = 201
    FolderNotExist = 202
    FileNotExist = 203
    SameFileExist = 204
    ServerLogicError = 300
    DbAccessSecretNotFound = 301


class WebApiException(BaseException):
    def __init__(self, status_code: int, error_code: ErrorCode, msg: str):
        self.status_code = status_code
        self.error_code = error_code
        self.msg = msg

    def create_error_response(self) -> dict:
        print("WebApiException={}".format(self))
        print("---traceback--------------------------------")
        traceback.print_exc(file=sys.stdout)
        print("-----------------------------------")
        return {
            "statusCode": self.status_code,
            "body": json.dumps(
                {
                    "errorCode": self.error_code.name,
                    "msg": self.msg,
                    "tb": traceback.format_exc().split("\n"),
                }
            ),
        }

    def create_update_for_status_db(self) -> dict:
        return {
            "$push": {
                "errors": {
                    "errorCode": self.error_code.name,
                    "msg": self.msg,
                    "tb": traceback.format_exc().split("\n"),
                }
            }
        }


# parameter 処理用のutil ----------------------------------------------------------------


class ApiGatewayEventAnalyzer:
    def __init__(self, event: dict):
        self.event = event

    def solve_params(self, params_dict: dict) -> dict:
        # params_dictは下記のフォーマットで記述
        # {
        #     param_name_1: {
        #         "where": （"path"もしくは"query"もしくは"body"）,
        #         "required": （TrueもしくはFalse）,
        #         "default": （デフォルト値を指定）,
        #         "type": [（型1）,（型2）,・・・略],
        #         "options": {（オプションを指定）}
        #     },
        #     param_name_2: {
        #         ・・・略
        #     },
        #     ・・・略
        # }
        #
        # ・whereが"path"もしくは"query"の場合、"type"は指定不要です（必ず[str]と見做されます）
        # ・whereが"body"の場合、bodyにセットされたJSON文字列がパースされた結果得られる辞書におけるキー／バリューについて、
        # 　バリューとして許す型を記載します（バリデーション用）
        # ・requiredを指定しない場合、Falseと見做します
        # ・requiredをTrueとした場合、paramが存在しないと例外をスローします
        # ・requiredをFalseとした場合、paramが存在しない場合にはdefaultで指定した値がparamの値として返されます。
        # 　・defaultを指定しない場合、存在しないparamの値はNoneになります
        # ・optionsを指定することで、型の変換ができます
        # ・例えば、whereが"path"もしくは"query"の場合、全てのパラメータはstr型として解釈されますが、
        #   optionsに{"convert_to_boolean":True}や{"convert_to_int":True}を指定することでboolean型やint型に変換できます。

        post_data = json.loads(self.event["body"]) if "body" in self.event else {}
        params = {}

        print("solve_params called:")
        print("path= {}".format(self.event.get("pathParameters")))
        print("query= {}".format(self.event.get("queryStringParameters")))
        print("body= {}".format(post_data))

        for key, spec in params_dict.items():
            options = spec["options"] if "options" in spec else {}
            if spec["required"]:
                options["raise_if_absent"] = True

            if spec["where"] == "path":
                value = self._get_path_parameter(key, **options)
            elif spec["where"] == "query":
                value = self._get_query_string_parameter(
                    key, spec["default"], **options
                )
            elif spec["where"] == "body":
                value = self._get_param_value(
                    post_data,
                    key,
                    spec["type"] if "type" in spec else None,
                    spec["default"] if "default" in spec else None,
                    **options,
                )
            params[key] = value
        return params

    @staticmethod
    def _get_param_value(
        dictionary: dict,
        key: str,
        type_list: list,
        default_value=None,
        *,
        convert_to_boolean: bool = False,
        convert_to_int: bool = False,
        convert_to_float: bool = False,
        convert_to_theta_phi: bool = False,
        raise_if_absent: bool = False,
        raise_if_blank_dict: bool = False,
        return_as_list: bool = False,
    ) -> Any:
        # get value
        if key not in dictionary:
            if raise_if_absent:
                raise WebApiException(
                    400, ErrorCode.ParamNotFound, "{} is neccesary".format(key)
                )
            val = default_value
        else:
            val = dictionary[key]

            # check value type
            if not _is_type(val, type_list):
                raise WebApiException(
                    400,
                    ErrorCode.BadFormatParam,
                    "type of {} must be {}".format(key, " or ".join(type_list)),
                )

            # covert value
            if isinstance(val, dict):
                if convert_to_theta_phi:
                    if "theta" in val and "phi" in val and len(val) == 2:
                        pass
                    elif "x" in val and "y" in val and "z" in val and len(val) == 3:
                        val = {  # convert
                            "theta": math.atan2(
                                math.sqrt(val["x"] ** 2 + val["y"] ** 2), val["z"]
                            ),
                            "phi": math.atan2(val["y"], val["x"]),
                        }
                    else:
                        raise WebApiException(
                            400,
                            ErrorCode.BadFormatParam,
                            "elements of {} must be thera&phi or x&y&z".format(key),
                        )
            elif isinstance(val, str):
                if convert_to_boolean:
                    if val == "true":
                        val = True
                    elif val == "false":
                        val = False
                    else:
                        raise WebApiException(
                            400,
                            ErrorCode.BadFormatParam,
                            "{}={} must be true or false".format(key, val),
                        )
                elif convert_to_int:
                    try:
                        val = int(val)
                    except Exception:
                        raise WebApiException(
                            400,
                            ErrorCode.BadFormatParam,
                            "{}={} must be int".format(key, val),
                        )
                elif convert_to_float:
                    try:
                        val = float(val)
                    except Exception:
                        raise WebApiException(
                            400,
                            ErrorCode.BadFormatParam,
                            "{}={} must be float".format(key, val),
                        )

        # error check
        if raise_if_blank_dict:
            if isinstance(val, list):
                for elem in val:
                    if isinstance(val, dict) and len(elem) == 0:
                        raise WebApiException(
                            400,
                            ErrorCode.BadFormatParam,
                            "{} must not contain blank dict".format(key),
                        )
            elif isinstance(val, dict):
                if len(val) == 0:
                    raise WebApiException(
                        400,
                        ErrorCode.BadFormatParam,
                        "{} must not be blank dict".format(key),
                    )

        # return value
        return [val] if return_as_list and isinstance(val, list) else val

    def _get_path_parameter(
        self,
        key: str,
        default_value=None,
        *,
        convert_to_boolean: bool = False,
        convert_to_int: bool = False,
        convert_to_float: bool = False,
        raise_if_absent: bool = True,
    ) -> str:
        return self._get_param_value(
            self.event["pathParameters"] if "pathParameters" in self.event else {},
            key,
            [str],
            default_value,
            convert_to_boolean=convert_to_boolean,
            convert_to_int=convert_to_int,
            convert_to_float=convert_to_float,
            raise_if_absent=raise_if_absent,
        )

    def _get_query_string_parameter(
        self,
        key: str,
        default_value=None,
        *,
        convert_to_boolean: bool = False,
        convert_to_int: bool = False,
        convert_to_float: bool = False,
        raise_if_absent: bool = False,
    ) -> str:
        return self._get_param_value(
            self.event["queryStringParameters"]
            if "queryStringParameters" in self.event
            else {},
            key,
            [str],
            default_value,
            convert_to_boolean=convert_to_boolean,
            convert_to_int=convert_to_int,
            convert_to_float=convert_to_float,
            raise_if_absent=raise_if_absent,
        )

    def get_user_email(self) -> str:
        try:
            return self.event["requestContext"]["authorizer"]["jwt"]["claims"]["email"]
        except Exception:
            return "unknown"


# データ型処理用のutil ----------------------------------------------------------------


def _is_type(val: Any, type_list: list) -> bool:
    for type in type_list:
        if isinstance(val, type):
            return True
    return False


def _convert_date_type(time: datetime) -> int:
    return int(time.timestamp() * 1000)


def _convert_value_to_json_serializable(val: Any) -> Any:
    if isinstance(val, datetime):
        return _convert_date_type(val)
    elif isinstance(val, Timestamp):
        return _convert_date_type(val.as_datetime())
    elif isinstance(val, ObjectId):
        return str(val)
    elif isinstance(val, dict):
        return {k: _convert_value_to_json_serializable(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [_convert_value_to_json_serializable(v) for v in val]
    else:
        return val


def convert_dict_to_json_serializable(dictionary: dict) -> dict:
    return {k: _convert_value_to_json_serializable(v) for k, v in dictionary.items()}


def convert_array_of_dict_to_json_serializable(documents: Sequence[dict]) -> list:
    return list(map(convert_dict_to_json_serializable, documents))


def convert_query_value(value: Any) -> Any:
    if type(value) == dict:
        if len(value) == 1 and "$toObjectId" in value:
            return ObjectId(value["$toObjectId"])
        elif len(value) == 1 and "$toDate" in value:
            if type(value["$toDate"]) == int:
                return datetime.fromtimestamp(value["$toDate"] / 1000)
            elif type(value["$toDate"]) == str:
                return datetime.fromisoformat(value["$toDate"])
            else:
                return value
        else:
            return {k: convert_query_value(v) for k, v in value.items()}
    elif type(value) == list:
        return [convert_query_value(v) for v in value]
    else:
        return value


# 各種AWSリソースアクセス用のutil ----------------------------------------------------------------


class DocumentDbAccess:
    DEFAULT_DB_NAME = "sample_database"
    ENVVAR_NAME_SECRETID = "SecretId"

    def __init__(
        self,
        db_name: str | None = None,
        secret_id: str | None = None,
        *,
        tls_ca_file: str | None = None,
    ):
        if db_name is None:
            db_name = self.DEFAULT_DB_NAME
        if secret_id is None and os.getenv("ENV") != "local":
            if self.ENVVAR_NAME_SECRETID not in os.environ:
                raise WebApiException(
                    500,
                    ErrorCode.DbAccessSecretNotFound,
                    "db access not permitted for this lambda",
                )
            secret_id = os.environ[self.ENVVAR_NAME_SECRETID]
        self.db: Database = self._create_db_client(secret_id, tls_ca_file=tls_ca_file)[
            db_name
        ]

    def create_collection(
        self,
        collection_name: str,
    ) -> None:
        try:
            self.db.create_collection(collection_name)
        except Exception as ex:
            print("exception when creating collection", ex)

    def get_collection(
        self, collection_name: str, raise_if_collection_not_exist: bool = False
    ) -> Collection:
        if (
            raise_if_collection_not_exist
            and collection_name not in self.db.list_collection_names()
        ):
            raise WebApiException(
                400,
                ErrorCode.CollectionNotExist,
                "Not found: collection={}".format(collection_name),
            )
        return self.db[collection_name]

    # 「データを削除しないdelete」「データを削除しないupdate」で隠されたdocumentsを除いたcountを行います。
    # deletedをFalseにした場合、「データを削除しないupdate」で隠されたdocumentsを除いて、
    # 「データを削除しないdelete」を含めたcountを行います。
    # soft_delete_update_modeをFalseにした場合、隠されたdocumentsも含め全てのcountを行います
    def count(
        self,
        collection_name: str,
        filter: dict,
        *,
        deleted: bool = False,
        raise_if_collection_not_exist: bool = False,
        soft_delete_update_mode: bool = True,
    ) -> int:
        if soft_delete_update_mode:
            if not deleted:
                filter["deleted"] = {"$ne": True}  # deletedが存在しない、もしくはFalseであるドキュメントに絞る
            filter["updated"] = {"$ne": True}  # updatedが存在しない、もしくはFalseであるドキュメントに絞る
        collection = self.get_collection(collection_name, raise_if_collection_not_exist)
        count = collection.count_documents(filter)
        return count

    # version毎の情報の集計値を取得します
    def stat_by_version(
        self,
        collection_name: str,
        filter: dict = {},
        *,
        raise_if_collection_not_exist: bool = False,
    ) -> dict[str, dict[str, int]]:
        collection = self.get_collection(collection_name, raise_if_collection_not_exist)

        stat_by_version = collection.aggregate(
            [
                {"$match": filter},
                {
                    "$group": {
                        "_id": "$version",
                        "creators": {"$addToSet": "$creator"},
                        "last_inserted": {"$max": "$inserted"},
                    }
                },
            ]
        )

        stat_by_version_and_deleted = collection.aggregate(
            [
                {"$match": filter},
                {
                    "$group": {
                        "_id": ["$version", "$deleted"],
                        "count": {"$sum": 1},
                    }
                },
            ]
        )

        count_dict = {
            line_dict["_id"]: {
                "creators": line_dict["creators"],
                "last_inserted": (
                    _convert_date_type(line_dict["last_inserted"])
                    if line_dict["last_inserted"] is not None
                    else None  # versionフィールドがないcollectiomもあるのでこの分岐が必要
                ),
                "count": 0,
                "deleted": 0,
                "updated": 0,
            }
            for line_dict in stat_by_version
        }

        for line_dict in stat_by_version_and_deleted:
            version, deleted = line_dict["_id"]
            if deleted is None:
                count_dict[version]["updated"] = line_dict["count"]
            elif not deleted:
                count_dict[version]["deleted"] = line_dict["count"]
            else:
                count_dict[version]["count"] = line_dict["count"]
        return count_dict

    def find(
        self,
        collection_name: str,
        filter: dict,
        *,
        projection: dict | None = None,
        sort: dict | list | str | None = None,
        limit: int | None = None,
        deleted: bool = False,
        raise_if_collection_not_exist: bool = False,
        soft_delete_update_mode: bool = True,
    ) -> Cursor:
        if soft_delete_update_mode:
            # 「データを削除しないdelete」「データを削除しないupdate」で隠されたdocumentsを除いたfindを行います
            if not deleted:
                filter["deleted"] = {"$ne": True}  # deletedが存在しない、もしくはFalseであるドキュメントに絞る
            filter["updated"] = {"$ne": True}  # updatedが存在しない、もしくはFalseであるドキュメントに絞る

        collection = self.get_collection(collection_name, raise_if_collection_not_exist)
        documents = collection.find(
            filter=filter,
            projection=projection,
        )

        if sort is not None:
            if isinstance(sort, dict):
                sort = list(sort.items())
            if (isinstance(sort, list) and len(sort) >= 1) or isinstance(sort, str):
                documents = documents.sort(sort)

        if limit is not None:
            documents = documents.limit(limit)
        return documents

    def find_one(
        self,
        collection_name: str,
        filter: dict,
        *,
        projection: dict | None = None,
        deleted: bool = False,
        raise_if_collection_not_exist: bool = False,
        soft_delete_update_mode: bool = True,
    ) -> dict | None:
        if soft_delete_update_mode:
            # 「データを削除しないdelete」「データを削除しないupdate」で隠されたdocumentsを除いたfindを行います
            if not deleted:
                filter["deleted"] = {"$ne": True}  # deletedが存在しない、もしくはFalseであるドキュメントに絞る
            filter["updated"] = {"$ne": True}  # updatedが存在しない、もしくはFalseであるドキュメントに絞る

        collection = self.get_collection(collection_name, raise_if_collection_not_exist)
        document = collection.find_one(
            filter=filter,
            projection=projection,
        )
        return document

    def soft_delete(
        self,
        collection_name: str,
        filter: dict,
        *,
        restore: bool = False,
        raise_if_collection_not_exist: bool = False,
    ) -> int:
        # 「データを削除しないdelete」を行います
        # 具体的には、delete対象のdocumentsのdeletedフィールドにtrueをセットすることで削除したとみなします
        # 削除したdocumentを元に戻すには、deletedフィールドにfalseをセットします

        filter["deleted"] = restore
        collection = self.get_collection(collection_name, raise_if_collection_not_exist)
        result = collection.update_many(
            filter=filter, update={"$set": {"deleted": not restore}}
        )
        return result.modified_count

    def soft_update_not_deleted_documents(
        self,
        collection_name: str,
        filter: dict,
        update: dict,
        creator: str,
        *,
        raise_if_collection_not_exist: bool = False,
        insert_if_not_found: bool = True,
    ) -> int:
        # 「データを削除しないupdate」を行います。
        # 具体的には、update対象のdocumentsを
        # ・deletedフィールドをunset
        # ・updatedフィールドをtrueに
        # して複製し残した上でupdateを行います
        # update対象はdeleteされていない最新のdocumentに限られます
        #
        # update方法詳細
        # 1. filterパラメータを用いてfindを実行し変更対象のdocumentを得る
        # 2. 1のdocumentに対しupdateを行いdeletedをunset、updatedをtrueにする
        # 3. 1で得られたdocumentをinsertする
        # 4. 3で追加したdocumentをupdateパラメータに従ってupdateする

        collection = self.get_collection(collection_name, raise_if_collection_not_exist)

        filter["deleted"] = False  # deleteされていない最新のdocumentに限定
        print("target:{}".format(filter))

        # 1. findを実行し変更対象のdocumentを得る
        print("1.--------------------------------------------")
        documents = list(collection.find(filter=filter))
        print("len(documents):{}".format(len(documents)))

        if len(documents) > 0:
            # 2. 1のdocumentに対しupdateを行いdeletedをunsetする（＝これが最新の１つ前のdocumentになる）
            print("2.--------------------------------------------")
            res = collection.update_many(
                filter=filter,
                update={"$unset": {"deleted": True}, "$set": {"updated": True}},
            )
            print("modified_count:{}".format(res.modified_count))

            # 3. 1で得られたdocumentをinsertする（＝これで最新のdocumentになる）
            print("3.--------------------------------------------")

            cnt = 0
            for document in documents:
                try:
                    base = {**document, "original": document["_id"]}
                    del base["_id"]
                    collection.insert_one(base)
                    cnt += 1
                except Exception:
                    print("fail to insert {}".format(document))
            print("insert {} documents".format(cnt))

            # 4. 3で追加したdocumentをupdateパラメータに従ってupdateする
            print("4.--------------------------------------------")
            collection.update_many(
                filter=filter,
                update=self.get_update_with_system_field(update, creator),
            )
            return len(documents)
        elif insert_if_not_found:
            collection.update_one(
                filter=filter,
                update=self.get_update_with_system_field(update, creator),
                upsert=True,
            )
            return 1
        else:
            return 0

    @staticmethod
    def get_update_with_system_field(update: dict, creator: str) -> dict:
        update_with_system_field = {**update}
        if "$set" not in update_with_system_field:
            update_with_system_field["$set"] = {}
        update_with_system_field["$set"]["creator"] = creator
        update_with_system_field["$set"]["inserted"] = datetime.utcnow()

        print("update:{}".format(update_with_system_field))
        return update_with_system_field

    def soft_replace_not_deleted_documents(
        self,
        collection_name: str,
        filter: dict,
        insertData: dict,
        creator: str,
        *,
        raise_if_collection_not_exist: bool = False,
        insert_if_not_found: bool = True,
    ) -> int:
        # 「データを削除しないreplace」を行います。
        # 具体的には、replace対象のdocumentsを
        # ・deletedフィールドをunset
        # ・updatedフィールドをTrueに
        # した上でinsertを行います
        # replace対象はdeleteされていない最新のdocumentに限られます

        collection = self.get_collection(collection_name, raise_if_collection_not_exist)

        filter["deleted"] = False  # deleteされていない最新のdocumentに限定

        documents = list(collection.find(filter=filter))

        if len(documents) > 0:
            collection.update_many(
                filter=filter,
                update={"$unset": {"deleted": True}, "$set": {"updated": True}},
            )

            for document in documents:
                collection.insert_one(
                    {
                        **insertData,
                        "deleted": False,
                        "original": document["_id"],
                        "creator": creator,
                        "inserted": datetime.utcnow(),
                    }
                )
            return len(documents)
        elif insert_if_not_found:
            collection.insert_one(
                {
                    **insertData,
                    "deleted": False,
                    "creator": creator,
                    "inserted": datetime.utcnow(),
                }
            )
            return 1
        else:
            return 0

    @staticmethod
    def _create_db_client(
        secret_id: str, *, tls_ca_file: str | None = None
    ) -> MongoClient:
        if os.getenv("ENV") == "local":
            # Settings for local DB
            db_info = {
                "username": os.environ["DB_USERNAME"],
                "password": os.environ["DB_PASSWORD"],
                "host": os.environ["DB_HOST"],
                "port": os.environ["DB_PORT"],
            }
            option = []
        else:
            # Settings for prod DB
            # Get SecretID from AWS Secrets Manager
            try:
                smclient = boto3.client(service_name="secretsmanager")
                get_secret_value_response = smclient.get_secret_value(
                    SecretId=secret_id
                )
            except ClientError as e:
                raise e

            db_info = json.loads(get_secret_value_response["SecretString"])
            option = [
                "tls=true",
                "replicaSet=rs0",
                "readPreference=secondaryPreferred",
                "retryWrites=false",
            ]
            if tls_ca_file is not None:
                option.append("tlsCAFile=" + tls_ca_file)

        # Create a MongoDB client, open a connection to Amazon DocumentDB as a replica set and specify the read preference as secondary preferred
        return MongoClient(
            "mongodb://{}:{}@{}:{}/?{}".format(
                str(db_info["username"]),
                str(db_info["password"]),
                str(db_info["host"]),
                str(db_info["port"]),
                "&".join(option),
            )
        )


class SqsAccess:
    REGION = "ap-northeast-1"
    ACCOUNT = "029495666331"
    ENDPOINT_URL = "https://sqs.ap-northeast-1.amazonaws.com"

    def __init__(self, queue_url: str):
        self.queue_url = queue_url

    def send_json_message(self, message_dict: dict) -> dict:
        return boto3.client("sqs", endpoint_url=self.ENDPOINT_URL).send_message(
            QueueUrl=self.queue_url, MessageBody=json.dumps(message_dict)
        )

    @classmethod
    def from_pj1_db_naming(
        cls, api_name: str, branch_name: str, func_name: str
    ) -> SqsAccess:
        return SqsAccess(
            "https://sqs.{}.amazonaws.com/{}/{}".format(
                cls.REGION,
                cls.ACCOUNT,
                "{}-{}-queue-{}_waiting".format(api_name, branch_name, func_name),
            )
        )


class S3BulkAccess:
    def __init__(
        self,
        bucket: str,
    ):
        self.client = S3Access.get_s3_client_for_web()
        self.bucket = bucket

    def get_presigned_url(
        self,
        key_or_key_list: list | str | None,
        expire_sec: int = 3600,
    ) -> list | str | None:
        if key_or_key_list is None:
            return None
        elif isinstance(key_or_key_list, str):
            return S3Access.generate_presigned_url(
                self.client, self.bucket, key_or_key_list, expire_sec
            )
        else:
            return [
                S3Access.generate_presigned_url(
                    self.client, self.bucket, key, expire_sec
                )
                for key in key_or_key_list
            ]

    def convert_key_to_url(self, key_dict: dict, *, expire_sec: int = 3600) -> dict:
        return {
            k: (
                self.convert_key_to_url(v)
                if isinstance(v, dict)
                else self.get_presigned_url(v, expire_sec)
            )
            for k, v in key_dict.items()
        }


class S3Access:
    ENDOPOINT_URL = os.environ.get(
        "ENDOPOINT_URL",
        "https://bucket.vpce-005eb6ef11fc3b348-3xjmav0g.s3.ap-northeast-1.vpce.amazonaws.com",
    )

    def __init__(self, bucket: str, key: str, region: str = "ap-northeast-1"):
        self.bucket = bucket
        self.key = key
        self.region = region
        self.s3_resource_for_vpc = self.get_s3_resource_for_vpc()

    @staticmethod
    def generate_presigned_url(
        client: S3Client, bucket: str, key: str, expire_sec: int = 3600
    ) -> str:
        return client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expire_sec,
            HttpMethod="GET",
        )

    def get_presigned_url(self, expire_sec: int = 3600) -> str:
        return self.generate_presigned_url(
            self.get_s3_client_for_web(), self.bucket, self.key, expire_sec
        )

    def exist_file(self) -> bool:
        try:
            self.get_s3_client_for_vpc().head_object(Bucket=self.bucket, Key=self.key)
            return True
        except ClientError as ex:
            if (
                ex.response["Error"]["Code"] == "404"
                or ex.response["Error"]["Code"] == "403"
            ):
                return False
            else:
                raise ex

    def exist_folder(
        self,
        *,
        check_any_file_exist: bool = False,
        check_direct_file_only: bool = False,
        check_file_depth: int = 1,
    ) -> bool:
        # フォルダがある場合、もしくはそのフォルダ以下のファイルがある場合にtrueを返す
        # 注）s3の実装上実際にはフォルダという概念はなく/を含むkey名でファイルを管理しているだけ。
        # 　　よって「フォルダ（=key名が/で終わる0バイトのファイル）」がないがフォルダ内のファイルのみがある場合が存在する
        #
        # check_any_file_existが設定されている場合
        # 　フォルダ以下のどこかにファイル（/以外でおわるキー）が存在しない場合falseを返す
        # check_any_file_exist及びcheck_direct_file_onlyが設定されている場合
        # 　check_file_depthで指定するフォルダ深さ（1の場合フォルダ直下を意味する）にファイル（/以外でおわるキー）が存在しない場合falseを返す
        #
        # 実装上の注意：ファイル数が大量にある場合に関数のレスポンスが遅くならないように

        self._assert_key_is_folder("target")

        for summary in self.s3_resource_for_vpc.Bucket(self.bucket).objects.filter(
            Prefix=self.key
        ):
            if check_any_file_exist:
                if not summary.key.endswith("/"):
                    if (not check_direct_file_only) or len(
                        summary.key[len(self.key) :].split("/")
                    ) == check_file_depth:
                        return True
            else:
                return True
        return False

    def copy_to(
        self,
        dest_bucket: str,
        dest_file_key: str,
    ) -> None:
        self.s3_resource_for_vpc.Object(dest_bucket, dest_file_key).copy(
            {
                "Bucket": self.bucket,
                "Key": self.key,
            }
        )

    def copy_children_to(
        self,
        dest_bucket: str,
        dest_folder_key: str,
        *,
        copy_src_folder_name: bool = True,
        extension_list: list | None = None,
        direct_children_only: bool = False,
    ) -> int:
        self._assert_key_is_folder("s3 copy source")

        if not dest_folder_key.endswith("/"):
            dest_folder_key += "/"

        omit_key_length = len(self.key)
        if copy_src_folder_name:
            omit_key_length -= len(self.key.split("/")[-2]) + 1

        cnt = 0
        for summary in self.s3_resource_for_vpc.Bucket(self.bucket).objects.filter(
            Prefix=self.key
        ):
            if summary.key == self.key:
                continue
            if (
                direct_children_only
                and len(summary.key[len(self.key) :].split("/")) > 1
            ):
                continue

            if extension_list is None or summary.key.split(".")[-1] in extension_list:
                dest_file_key = dest_folder_key + summary.key[omit_key_length:]
                self.s3_resource_for_vpc.Object(dest_bucket, dest_file_key).copy(
                    {
                        "Bucket": self.bucket,
                        "Key": summary.key,
                    }
                )
                cnt += 1
        return cnt

    def download_children(
        self,
        dest_dir_path: str,
        *,
        copy_src_folder_name: bool = True,
        extension_list: list | None = None,
    ) -> int:
        self._assert_key_is_folder("s3 copy source")

        if not dest_dir_path.endswith("/"):
            dest_dir_path += "/"
        os.makedirs(dest_dir_path, exist_ok=True)

        omit_key_length = len(self.key)
        if copy_src_folder_name:
            omit_key_length -= len(self.key.split("/")[-2]) + 1

        cnt = 0
        for summary in self.s3_resource_for_vpc.Bucket(self.bucket).objects.filter(
            Prefix=self.key
        ):
            if summary.key.endswith("/"):
                continue
            if extension_list is None or summary.key.split(".")[-1] in extension_list:
                self.s3_resource_for_vpc.Object(self.bucket, summary.key).download_file(
                    dest_dir_path + summary.key[omit_key_length:]
                )
                cnt += 1
        return cnt

    def upload_children(
        self,
        src_dir_path: str,
        *,
        extension_list: list | None = None,
        delete_after_uploaded: bool = False,
    ) -> int:
        self._assert_key_is_folder("s3 copy destination")

        if not src_dir_path.endswith("/"):
            src_dir_path += "/"

        cnt = 0
        for path in glob("{}*".format(src_dir_path)):
            if extension_list is None or path.split(".")[-1] in extension_list:
                self.s3_resource_for_vpc.Bucket(self.bucket).upload_file(
                    path, Key="{}{}".format(self.key, os.path.basename(path))
                )
                cnt += 1
                if delete_after_uploaded:
                    os.remove(path)

        return cnt

    def _assert_key_is_folder(self, target_name: str) -> None:
        if not self.key.endswith("/"):
            raise WebApiException(
                400,
                ErrorCode.BadFormatParam,
                "{} should be folder name that ends /".format(target_name),
            )

    def find_files_from_children(
        self,
        ext_list: list,
        *,
        raise_if_not_exist: bool = True,
        return_as_one_key: bool = False,
    ) -> list | str | None:
        self._assert_key_is_folder("serach target")

        key_list = []
        for summary in self.s3_resource_for_vpc.Bucket(self.bucket).objects.filter(
            Prefix=self.key
        ):
            if summary.key.split(".")[-1] in ext_list:
                key_list.append(summary.key)

        if len(key_list) == 0:
            if raise_if_not_exist:
                raise WebApiException(
                    500,
                    ErrorCode.FileNotExist,
                    "target folder does not includes any file which name ends {}".format(
                        ext_list
                    ),
                )
            else:
                return None if return_as_one_key else []
        else:
            if return_as_one_key:
                if len(key_list) > 1:
                    raise WebApiException(
                        500,
                        ErrorCode.FileNotExist,
                        "target folder includes more than 1 files which name ends .{}".format(
                            ext_list
                        ),
                    )
                else:
                    return key_list[0]
            else:
                return key_list

    def get_direct_child_folder_urls(self) -> list:
        self._assert_key_is_folder("target")

        result = self.get_s3_client_for_vpc().list_objects(
            Bucket=self.bucket, Prefix=self.key, Delimiter="/"
        )
        if "CommonPrefixes" in result:
            return [
                "s3://{}/{}".format(self.bucket, common["Prefix"])
                for common in result["CommonPrefixes"]
            ]
        else:
            return []

    def get_s3_url(self) -> str:
        return "s3://{}/{}".format(self.bucket, self.key)

    @staticmethod
    def from_s3_location_url(url_or_path: str, *, bucket_for_path=None) -> S3Access:
        # e.g. https://robocip-db-data.s3.ap-northeast-1.amazonaws.com/robocip/test.txt
        m = re.match(
            r"^https?://([^\.]+)\.s3\.([^\.]+)\.amazonaws\.com/(.+)$", url_or_path
        )
        if m is not None:
            bucket, region, key = m.groups()
            return S3Access(bucket, key, region)

        # e.g. https://robocip-db-data.s3-ap-northeast-1.amazonaws.com/robocip/test.txt
        m = re.match(
            r"^https?://([^\.]+)\.s3-([^\.]+)\.amazonaws\.com/(.+)$", url_or_path
        )
        if m is not None:
            bucket, region, key = m.groups()
            return S3Access(bucket, key, region)

        # e.g. https://robocip-db-data.amazonaws.com/robocip/test.txt
        m = re.match(r"^https?://([^\.]+)\.amazonaws\.com/(.+)$", url_or_path)
        if m is not None:
            bucket, key = m.groups()
            return S3Access(bucket, key)

        # e.g. s3://robocip-db-data/robocip/test.txt
        m = re.match("^s3://([^/]+)/(.+)$", url_or_path)
        if m is not None:
            bucket, key = m.groups()
            return S3Access(bucket, key)

        # e.g. robocip/test.txt
        return S3Access(bucket_for_path, url_or_path)

    @classmethod
    def get_s3_resource_for_vpc(cls) -> S3ServiceResource:
        return boto3.resource("s3", endpoint_url=cls.ENDOPOINT_URL)

    @classmethod
    def get_s3_resource_for_web(cls) -> S3ServiceResource:
        return boto3.resource("s3")

    @classmethod
    def get_s3_client_for_vpc(cls) -> S3Client:
        return boto3.client("s3", endpoint_url=cls.ENDOPOINT_URL)

    @classmethod
    def get_s3_client_for_web(cls) -> S3Client:
        return boto3.client("s3")


class DbUpdater:
    def __init__(
        self,
        db_name: str,
        secret_id: str,
        collection_name: str,
        filter: dict,
        *,
        tls_ca_file: str | None = None,
    ):
        self.db = DocumentDbAccess(db_name, secret_id, tls_ca_file=tls_ca_file)
        self.collection = self.db.get_collection(collection_name)
        self.filter = filter

    def update_one(
        self,
        update: dict,
    ) -> UpdateResult:
        return self.collection.update_one(
            self.filter,
            update,
        )
