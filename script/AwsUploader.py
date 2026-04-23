from __future__ import annotations

import base64
import glob
import hashlib
import os
import shutil
import subprocess

import boto3
from mypy_boto3_ecr import ECRClient  # from boto3-stubs
from mypy_boto3_lambda import LambdaClient  # from boto3-stubs

try:
    from docker.client import DockerClient
    from docker.errors import BuildError

    DOCKER_NOT_FOUND = False
except Exception:
    print("docker python package not found")
    DOCKER_NOT_FOUND = True


class Logger:
    msgs: list[str] = []

    def __init__(self, print_on_append=True, flush=True):
        self.print_on_append = print_on_append
        self.flush = flush

    def append(self, msg: str):
        if self.print_on_append:
            print(msg, flush=self.flush)
        self.msgs.append(msg)

    def dequeue_all(self) -> list[str]:
        msgs = self.msgs
        self.msgs = []
        return msgs

    def print_all(self):
        for msg in self.dequeue_all():
            print(msg, flush=self.flush)


LOGGER = Logger()


class CognitoAuth:
    def __init__(
        self,
        user_name: str,
        password: str,
        client_id: str,
        user_pool_id: str,
        identity_pool_id: str,
        region: str,
    ):
        id_token = self._get_id_token(user_name, password, client_id, region)
        self.session = self._get_session(
            id_token, user_pool_id, identity_pool_id, region
        )

    @staticmethod
    def _get_id_token(user_email: str, password: str, client_id: str, region: str):
        idp_client = boto3.client("cognito-idp", region)
        return idp_client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": user_email,
                "PASSWORD": password,
            },
            ClientId=client_id,
        )["AuthenticationResult"]["IdToken"]

    @staticmethod
    def _get_session(id_token, user_pool_id: str, identity_pool_id: str, region: str):
        login_info = {
            "cognito-idp.{}.amazonaws.com/{}".format(region, user_pool_id): id_token
        }
        identity_client = boto3.client("cognito-identity", region)

        identity_id = identity_client.get_id(
            IdentityPoolId=identity_pool_id,
            Logins=login_info,
        )["IdentityId"]

        credentials = identity_client.get_credentials_for_identity(
            IdentityId=identity_id,
            Logins=login_info,
        )["Credentials"]
        session = boto3.Session(
            credentials["AccessKeyId"],
            credentials["SecretKey"],
            credentials["SessionToken"],
            region,
        )
        return session


class AwsUploader:
    def __init__(
        self,
        repo_name: str,
        branch_name: str,
        ecr_name: str | None,
        *,
        lambda_client: LambdaClient | None = None,
        ecr_client: ECRClient | None = None,
    ):
        self.repo_name = repo_name.split("/")[-1] if "/" in repo_name else repo_name
        LOGGER.append("- repository name : {}".format(self.repo_name))
        self.branch_name = branch_name
        self.ecr_name = ecr_name

        self.lambda_client: LambdaClient = (
            lambda_client if lambda_client is not None else boto3.client("lambda")
        )
        self.ecr_client: ECRClient = (
            ecr_client if ecr_client is not None else boto3.client("ecr")
        )

        if not DOCKER_NOT_FOUND:
            self.docker_client = (
                DockerClient.from_env()  # pyright: ignore[reportPossiblyUnboundVariable]
            )
            self._docker_login()

    @staticmethod
    def from_cognito_auth(
        user_name: str,
        password: str,
        client_id: str,
        user_pool_id: str,
        identity_pool_id: str,
        region: str,
        repo_name: str,
        branch_name: str,
        ecr_name: str | None,
    ):
        auth = CognitoAuth(
            user_name,
            password,
            client_id,
            user_pool_id,
            identity_pool_id,
            region,
        )
        lambda_client: LambdaClient = auth.session.client("lambda")
        ecr_client: ECRClient = auth.session.client("ecr")

        return AwsUploader(
            repo_name,
            branch_name,
            ecr_name,
            lambda_client=lambda_client,
            ecr_client=ecr_client,
        )

    @staticmethod
    def _create_zip_file(out_path_without_ext, target_dir):
        subprocess.run(
            "deterministic_zip {}.zip *".format(out_path_without_ext),
            shell=True,
            cwd=target_dir,
        )

        # 1.code below does not work because make_archive add timestamp as extra header to zip, so hash vary every time
        # shutil.make_archive(
        #     out_path_without_ext,
        #     "zip",
        #     target_dir
        # )

        # 2.code below does not work also
        # subprocess.run("zip -o -X {} -r *".format(out_path_without_ext),shell=True,cwd=target_dir)

    def _update_lambda(self, lambda_func_name, zip_binary) -> str | None:
        hash = base64.b64encode(hashlib.sha256(zip_binary).digest()).decode()

        try:
            info = self.lambda_client.get_function(
                FunctionName=lambda_func_name,
            )
        except Exception:
            LOGGER.append(
                "- {}\n    --> server not ready. skip upload".format(lambda_func_name)
            )
            return None

        if info["Configuration"].get("CodeSha256") == hash:
            LOGGER.append(
                "- {}\n    --> code is not changed. skip upload".format(
                    lambda_func_name
                )
            )
            return None
        LOGGER.append(
            "- {}\n    --> start uploadig. hash={}".format(lambda_func_name, hash)
        )
        response = self.lambda_client.update_function_code(
            FunctionName=lambda_func_name,
            ZipFile=zip_binary,
            Publish=True,
        )
        return response["LastModified"]

    def upload_function(
        self, func_name, func_dir_path, temp_dir_path, common_dir_path=None
    ) -> str | None:
        lambda_func_name = "{}-{}-{}".format(
            self.repo_name, self.branch_name, func_name
        )
        if len(glob.glob(os.path.join(func_dir_path, "*"))) == 0:
            LOGGER.append("- {}\n    --> blank folder. skip upload".format(func_name))
            return None
        if common_dir_path is not None and os.path.exists(common_dir_path):
            shutil.copytree(
                common_dir_path,
                os.path.join(func_dir_path, "common"),
            )
        self._create_zip_file(os.path.join(temp_dir_path, func_name), func_dir_path)

        bin = open(os.path.join(temp_dir_path, func_name + ".zip"), "rb").read()
        result = self._update_lambda(
            lambda_func_name,
            bin,
        )
        return result

    def upload_functions(self, root_dir_path, common_dir_path=None) -> dict:
        # レポジトリ中のファイルを用いて各ラムダ関数を更新します
        # ★ポイント
        # - commonに入っているファイルは、各ラムダ関数の共通ファイルとして各ラムダ関数内のcommonフォルダにコピーされます
        #
        # 元のフォルダ構成
        # - lambda
        #  - (lambda名1)
        #   - api
        #    - lambda_function.py
        #    - (ライブラリなどの任意のファイル・フォルダ)
        #  - (lambda名2)
        #   - （略）
        # - common
        #  - (utilなどの任意のファイル・フォルダ)
        #
        # 上記の構成から下記のようにラムダ関数を生成します
        # (lambda名1)のラムダの中身は下記構成になります
        # - api
        #  - lambda_function.py
        #  - (ライブラリなどの任意のファイル・フォルダ)
        # - common
        #  - (utilなどの任意のファイル・フォルダ)

        if not os.path.exists(root_dir_path):
            return {}

        LOGGER.append("- stage name : {}".format(self.branch_name))
        LOGGER.append(
            "- start to update Lambdas using code per func under {}".format(
                root_dir_path
            )
        )
        if common_dir_path is not None and os.path.exists(common_dir_path):
            LOGGER.append("  - using common library under {}".format(common_dir_path))

        temp_dir_path = os.path.join(
            os.path.dirname(__file__), "temp", self.branch_name
        )
        os.makedirs(temp_dir_path, exist_ok=True)
        try:
            modified_dict = {}
            for func_dir_path in glob.glob(os.path.join(root_dir_path, "*")):
                func_name = os.path.basename(func_dir_path)
                modified = self.upload_function(
                    func_name, func_dir_path, temp_dir_path, common_dir_path
                )
                if modified is not None:
                    modified_dict[func_name] = modified
        finally:
            shutil.rmtree(temp_dir_path)

        LOGGER.append(
            "- COMPLETED! {} server functions modified".format(len(modified_dict))
        )
        for func_name, modified in modified_dict.items():
            LOGGER.append("    --> {} : {}".format(func_name, modified))
        return modified_dict

    def upload_containers(
        self,
        root_dir_path: str,
        common_dir_path: str | None = None,
        *,
        build_args: dict = {},
    ):
        # レポジトリ中のファイルを用いて下記を更新します
        # ・AWS Batch用のコンテナ
        # ・AWS Batchの状態変更イベントハンドリング用のラムダ関数
        # ★ポイント
        # - commonに入っているファイルは、各ラムダ関数の共通ファイルとして各ラムダ関数内のcommonフォルダにコピーされます
        #
        # 元のフォルダ構成
        # - docker
        #  - (バッチ名1)
        #   - Dockerfile
        #   - (Dockerfileから使用される任意のファイル・フォルダ)
        #   - status_change
        #    - api
        #     - lambda_function.py
        #     - (ライブラリなどの任意のファイル・フォルダ)
        #  - (バッチ名2)
        #    - （略）
        # - common
        #  - (utilなどの任意のファイル・フォルダ)
        #
        # 上記の構成から下記のようにラムダ関数を生成します
        # (バッチ名1)の状態変更ラムダの中身は下記構成になります
        # - api
        #  - lambda_function.py
        #  - (ライブラリなどの任意のファイル・フォルダ)
        # - common
        #  - (utilなどの任意のファイル・フォルダ)
        if not os.path.exists(root_dir_path):
            return {}
        if self.ecr_name is None:
            raise Exception("please set ECR_REGISTRY secret to use AWS Batch")

        temp_dir_path = os.path.join(
            os.path.dirname(__file__), "temp", self.branch_name
        )
        os.makedirs(temp_dir_path, exist_ok=True)
        for dir in glob.glob(os.path.join(root_dir_path, "*")):
            func_name = os.path.basename(dir)
            self.upload_container(
                dir,
                "{}/{}-{}:{}".format(
                    self.ecr_name, self.repo_name, func_name, self.branch_name
                ),
                build_args=build_args,
            )
            lambda_dir = os.path.join(dir, "status_change")
            if os.path.exists(lambda_dir):
                self.upload_function(
                    func_name, lambda_dir, temp_dir_path, common_dir_path
                )

    def upload_container(
        self, docker_file_folder_path: str, tag: str, *, build_args: dict = {}
    ):
        if DOCKER_NOT_FOUND:
            raise Exception("docker python module not found")

        LOGGER.append("- docker build : {}".format(tag))
        try:
            self.docker_client.images.build(
                path=docker_file_folder_path, tag=tag, buildargs=build_args, rm=True
            )
        except BuildError as ex:  # pyright: ignore[reportPossiblyUnboundVariable]
            LOGGER.append("error occur when running `docker build`")
            raise Exception(
                list(ex.build_log)
            )  # github actionsのログ上で見えるように変換する

        LOGGER.append("- docker push : {}".format(tag))
        self.docker_client.images.push(tag)
        LOGGER.append("- docker remove : {}".format(tag))
        # self.docker_client.containers.prune()  # build時にrm=Trueにしておけば不要なのでコメントアウト
        self.docker_client.images.remove(
            tag
        )  # github actions のdisk容量が少ない為、削除する
        self.docker_client.images.prune()  # github actions のdisk容量が少ない為、削除する

    def _docker_login(self):
        token = self.ecr_client.get_authorization_token()
        auth_data = token["authorizationData"][0]
        if "authorizationToken" in auth_data and "proxyEndpoint" in auth_data:
            username, password = (
                base64.b64decode(auth_data["authorizationToken"]).decode().split(":")
            )
            self.docker_client.login(
                username, password, registry=auth_data["proxyEndpoint"]
            )
            return True
        return False
