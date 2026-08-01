#!/bin/sh
set -eu

alias_name=local
policy_name=eduagents-app

mc alias set "$alias_name" http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "$alias_name/$MINIO_BUCKET"

if ! mc admin policy info "$alias_name" "$policy_name" >/dev/null 2>&1; then
  printf '%s\n' \
    '{"Version":"2012-10-17","Statement":[' \
    '{"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket","s3:ListBucketMultipartUploads"],"Resource":["arn:aws:s3:::'"$MINIO_BUCKET"'"]},' \
    '{"Effect":"Allow","Action":["s3:AbortMultipartUpload","s3:DeleteObject","s3:GetObject","s3:ListMultipartUploadParts","s3:PutObject"],"Resource":["arn:aws:s3:::'"$MINIO_BUCKET"'/*"]}' \
    ']}' >/tmp/eduagents-policy.json
  mc admin policy create "$alias_name" "$policy_name" /tmp/eduagents-policy.json
fi

if ! mc admin user info "$alias_name" "$MINIO_APP_ACCESS_KEY" >/dev/null 2>&1; then
  mc admin user add "$alias_name" "$MINIO_APP_ACCESS_KEY" "$MINIO_APP_SECRET_KEY"
fi

mc admin policy attach "$alias_name" "$policy_name" --user "$MINIO_APP_ACCESS_KEY"
echo "minio initialization complete: bucket=$MINIO_BUCKET user=$MINIO_APP_ACCESS_KEY policy=$policy_name"
