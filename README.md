# aws-api-template

- AWS上にWebAPIを実装するためのテンプレート
- Lambda関数もしくはdockerコンテナとしてWebAPIをデプロイできる

## 想定するフォルダ構成

- 下記フォルダ構成の前提でCI/CDが組まれています。★の位置に必要に応じて任意のファイルやフォルダを格納します。
    - (function name)のフォルダ１つが１つのLambda関数もしくは１つのdockerコンテナに相当
    - Lambdaのリソースは事前にハンドラーとして「api.lambda_function.lambda_handler」を設定して作成されていることを前提としている

```
- .github
    - （編集・削除しないこと）
- script
    - （編集・削除しないこと）
- common
    - ★
- lambda
    - (function name)
        - api
            - lambda_function.py
            - ★
        - ★
- docker
    - (function name)
        - Dockerfile
        - status_change
          - api
            - lambda_function.py
            - ★
          - ★
        - ★
```

説明

|フォルダ|説明|
|-|-|
|.github|ソースをpushした時にAWS上にデプロイを行う為のgithub actionsの設定を格納（変更不可）|
|script|github actions上で実行されるpythonスクリプトを格納（変更不可）|
|common|各Lambda関数から共通的に使用するutilスクリプトなどを格納|
|lambda|Lambda関数のコードを(function name)毎に格納（複数可）|
|docker|dockerコンテナの定義を(function name)毎に格納（複数可）|
|status_change|dockerコンテナをAWS Batchで実行した際のジョブ状態が変わったタイミングで呼ばれるLambda関数のコードを格納する。status_changeフォルダがない場合はcdkで指定されるデフォルトのLambda関数が実行される|

### このテンプレートのフォルダ構成

このテンプレートは、1つのlambda関数をpythonで実装する場合の例となっている

## 事前準備

1. secret設定
  - 下記のsecret設定がGithub上で必要
    - ROLE_ARN
      - AWSにアクセスする為のIAMロールのARNを指定する
      - このロールには、lambdaやECRを書き換える権限を与えておくこと
      - 例： arn:aws:iam::{account-id}:role/{role-name}
    - _GITHUB_USER
      - このレポジトリにアクセスする為のユーザー名を設定
      - この値は本レポジトリがgit submoduleで使用するレポジトリをcloneする為にgithub action中で利用
    - _GITHUB_PAT
      - このレポジトリにアクセスする為のPAT(personal access token)を設定
      - この値は本レポジトリがgit submoduleで使用するレポジトリをcloneする為にgithub action中で利用
  - WEB APIをdockerコンテナで作成する場合は下記も必要
    - ECR_REGISTRY
      - dockerコンテナを格納するECRのアドレスを指定する
      - 例： {id}.dkr.ecr.{region}.amazonaws.com
  - （任意）デプロイ状況をslackに通知したい場合は下記も設定
    - SLACK_WEBHOOK_URL
      - webhookのurlを指定
2. AWSリソース準備
   - AWS上にLamnda関数もしくはECR/AWS Batchのリソースを用意する（別途cdk等で作成すること）

## デプロイ
ソースコードを作成し、レポジトリにpushするとgithub actionのトリガーがかかり自動的にAWSにデプロイされます

### Lambdaのコードについて
- 最終的にデプロイされるLambdaの中身は「lambda/（関数名）」と「common」フォルダの中身から下記となります
    - api
      - lambda_function.py
      - (任意のファイル・フォルダ)
    - common
      - (任意のファイル・フォルダ)
