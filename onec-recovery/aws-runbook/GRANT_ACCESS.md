# Granting temporary access to the 1C host

Goal: give Claude a **short-lived, least-privilege** credential that can open a
shell on one specific EC2 instance via SSM Session Manager — no inbound port, no
SSH key, fully logged in CloudTrail, and it expires on its own.

Set these first:

```bash
export REGION=eu-central-1                 # your region
export INSTANCE_ID=i-0123456789abcdef0     # the 1C server
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

## 0. Check the instance is reachable by SSM

```bash
aws ssm describe-instance-information --region $REGION \
  --query "InstanceInformationList[?InstanceId=='$INSTANCE_ID']"
```

Empty result means the SSM agent isn't running or the instance lacks an
instance profile with `AmazonSSMManagedInstanceCore`. Fix that first — otherwise
there's nothing to connect to.

## 1. Create the scoped policy

```bash
cat > /tmp/claude-1c-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StartSessionOnOneInstance",
      "Effect": "Allow",
      "Action": ["ssm:StartSession"],
      "Resource": [
        "arn:aws:ec2:${REGION}:${ACCOUNT_ID}:instance/${INSTANCE_ID}",
        "arn:aws:ssm:${REGION}::document/AWS-StartInteractiveCommand",
        "arn:aws:ssm:${REGION}::document/SSM-SessionManagerRunShell"
      ]
    },
    {
      "Sid": "ManageOwnSession",
      "Effect": "Allow",
      "Action": ["ssm:TerminateSession", "ssm:ResumeSession"],
      "Resource": "arn:aws:ssm:*:*:session/\${aws:username}-*"
    },
    {
      "Sid": "ReadOnlyLookup",
      "Effect": "Allow",
      "Action": ["ssm:DescribeInstanceInformation", "ec2:DescribeInstances"],
      "Resource": "*"
    }
  ]
}
EOF

aws iam create-policy --policy-name Claude1CAnalysis \
  --policy-document file:///tmp/claude-1c-policy.json
```

## 2. Create a role Claude can assume

```bash
cat > /tmp/claude-1c-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::${ACCOUNT_ID}:root"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role --role-name Claude1CAnalysis \
  --assume-role-policy-document file:///tmp/claude-1c-trust.json \
  --max-session-duration 14400

aws iam attach-role-policy --role-name Claude1CAnalysis \
  --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/Claude1CAnalysis
```

## 3. Mint 4-hour credentials

```bash
aws sts assume-role \
  --role-arn arn:aws:iam::${ACCOUNT_ID}:role/Claude1CAnalysis \
  --role-session-name claude-1c \
  --duration-seconds 14400 \
  --query Credentials
```

Send the four values back, plus `REGION` and `INSTANCE_ID`:

```
AccessKeyId, SecretAccessKey, SessionToken, Expiration
```

They die after 4 hours on their own. To kill them sooner:

```bash
aws iam delete-role-policy --role-name Claude1CAnalysis --policy-name Claude1CAnalysis 2>/dev/null
aws iam detach-role-policy --role-name Claude1CAnalysis \
  --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/Claude1CAnalysis
aws iam delete-role --role-name Claude1CAnalysis
```

## Cleaner alternative — no credentials at all

Put them in the environment instead of the chat: **Claude Code → environment
settings → environment variables** (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`, `AWS_REGION`). They never appear in the transcript. Caveat:
environment changes apply to **new** sessions, so we'd continue in a fresh one.

See https://code.claude.com/docs/en/claude-code-on-the-web for how environments
are configured.

## Or skip access entirely

`01_restore_export.sh` + `02_aggregate.py` in this folder do the whole job. You
run two commands, send back a ~100 KB aggregates folder, and no credentials
change hands at all. Same end result.
