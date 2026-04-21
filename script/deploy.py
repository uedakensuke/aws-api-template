import os
import sys

from AwsUploader import AwsUploader

# ------------- CAUTION -------------
# stdout automatically send to SLACK when this script run in GitHub
# please take care to print messages as formatted easy to read in SLACK.
# -----------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise Exception(
            "ARG ERROR: deploy.py <repository_name> <branch_name> [<ecr_registry>]\n sys.argc={}".format(
                sys.argv
            )
        )
    repository_name, branch_name = sys.argv[1:3]    
    ecr_registry=sys.argv[3] if len(sys.argv)>=4 else None

    aws = AwsUploader(
        repository_name,
        branch_name,
        ecr_registry,
    )

    common_dir_path = os.path.join(os.path.dirname(__file__), "..", "common")
    lambda_dir_path = os.path.join(os.path.dirname(__file__), "..", "lambda")
    docker_dir_path = os.path.join(os.path.dirname(__file__), "..", "docker")

    if os.path.exists(lambda_dir_path):
        aws.upload_functions(lambda_dir_path, common_dir_path)

    if os.path.exists(docker_dir_path):
        if "GITHUB_USER" not in os.environ or "GITHUB_PAT" not in os.environ:
            raise Exception(
                "github user/pat(parsonal access token) is blank. please set environment variables GITHUB_USER and GITHUB_PAT"
            )

        aws.upload_containers(
            docker_dir_path,
            common_dir_path,
            build_args={
                "GITHUB_USER": os.environ["GITHUB_USER"],
                "GITHUB_PAT": os.environ["GITHUB_PAT"],
                "BRANCH": branch_name,
            },
        )
