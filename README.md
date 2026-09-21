# csi: CloudShell Interface

`csi` is a command-line interface for [AWS CloudShell](https://aws.amazon.com/cloudshell) which brings CloudShell to your terminal where it belongs.

It allows you to manage and connect to VPC and non-VPC CloudShell sessions directly from your command line.

## Key features

* **List and manage** CloudShell environments
* **Create VPC environments** with specific VPC, subnets, and security groups
* **Connect** to CloudShell environments via SSM in the terminal
* **Download and upload files** between your machine and CloudShell environments
* **Execute commands** remotely on CloudShell environments
* **Genie** - magically creates a CloudShell environment with the right network access to reach:
    * ENIs/hostnames/IP addresses on specific ports and IP protocols
    * EC2 instances
    * RDS databases

Each command and flag has tab completion (where needed).

<details><summary><b>Click here to see demo</b></summary>
   
![demo 18 70](https://github.com/user-attachments/assets/d0476469-865f-4477-b1db-e3c08cadc812)
</details>

## Why use csi?

Unfortunately, CloudShell is only available on the AWS console. There's no official support in the AWS CLI or any AWS SDK.

The only way to use CloudShell outside of the console is by making [sigv4](https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html) signed requests to the correct endpoints.

`csi` handles all these requests for you and provides a sleek interface with custom commands to make CloudShell easier to use.

## Why care about CloudShell?

In June 2024, Amazon announced the ability to spin up CloudShell environments in a VPC, subnets, and security groups of your choice.

This is extremely useful for troubleshooting issues:
* boot time is about half a minute, much faster than spinning up an ec2
* environments are ephemeral, which can be useful for testing and quick tasks
* you only pay for data transfer, [no additional fees](https://aws.amazon.com/cloudshell/pricing)

## Setup

### This fork: secret handling

Use Python 3.13+ and `uv sync --frozen` to install the reviewed dependency
versions and hashes from `uv.lock`. The Session Manager plugin must be at
least **1.2.536.0**; older versions are rejected.

```bash
export AWS_PROFILE=my-profile AWS_DEFAULT_REGION=us-east-1
uv run --frozen csi create --name investigation \
  --subnets subnet-example --security-groups sg-example
uv run --frozen csi execute investigation --stdin --timeout 60 < read-only-script.sh
uv run --frozen csi delete investigation
```

For sensitive input, pipe a script producer into `execute --stdin` rather
than putting secrets in `-c`, shell history, or a saved script. The runner
waits until terminal echo and shell history are disabled before sending the
script. It runs Bash with startup files disabled. Command output (including
remote stderr) goes to local stdout, with terminal control codes removed;
the exit code is preserved. Output intentionally printed by a script is not
redacted, so do not print tokens, environment contents, or raw secret responses.

Session credentials go through the plugin's temporary child environment, not
process arguments. The supported AWS plugin consumes and unsets that variable.
This prevents ordinary command-line exposure, not inspection by the same OS
user or root. Keep plugin diagnostic logging disabled for sensitive sessions.

`execute` does not upload local AWS credentials. Interactive `ssm` also skips
credential upload by default; `ssm --upload-credentials` explicitly enables the
existing AWS federation flow. `genie` does not enable credential upload.

Environment creation returns its ID immediately. Execution waits internally
for readiness (up to 180 seconds), logging only state changes. `--timeout`
controls the subsequent session/command deadline. Failed operations do not
print request/session contents or Python tracebacks. Sessions are closed after
execution, including failures; delete your temporary environment explicitly
and verify AWS has removed its network interface. A failed session close is
reported rather than silently treated as confirmed cleanup.

Validation:

```bash
uv run --frozen python -m unittest discover -s tests -v
uvx pip-audit --path .venv/lib/python3.13/site-packages --skip-editable
```

You can install `csi` via `pip` or `uv`, etc:

```bash
uv tool install cloudshell-interface
pip3 install cloudshell-interface
```

And run `csi --help` in your terminal

If you want tab completion, completion files are in [completions/](./completions/):
* For `bash`, put `csi.bash` in `$XDG_DATA_HOME/bash-completion/` or `~/.local/share/bash-completion/` if `$XDG_DATA_HOME` is not set
    * e.g. `ln -srf completions/csi.bash ~/.local/share/bash-completion/`
* For `zsh`, put `csi.zsh` somewhere in your `fpath` as `_csi`
    * e.g. `ln -srf completions/csi.zsh /path/to/fpath/_csi`
* **NOTE** to complete certain flags, tab completion depends on `python3`, `boto3`, `csi`, and `awk`

To install from the repository:

1. Install dependencies using `uv` or `pip`
2. If you wish to use `csi ssm`, `csi execute`, or `csi genie`, you **must** have the [AWS Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) somewhere in your `PATH`
3. Set your the AWS profile, e.g. via `AWS_PROFILE`
4. Run `bin/csi`
5. Symlink `bin/csi` on your `PATH`, or add `/path/to/repo/bin/` to your `PATH` to run `csi` globally

## Warnings

* This tool is not an official tool by Amazon/AWS
* Beware of the [service quotas for CloudShell](https://docs.aws.amazon.com/general/latest/gr/cloudshell.html#limits_cloudshell), specifically the adjustable 200 monthly hour limit **applied across all** IAM principals within an account.
* This tool is [GPLv3 licensed](./LICENSE) - there is no warranty. If you reach service limits in your account, contact AWS support.
* CloudShell environments exist per IAM principal. When assuming a role, make sure to do so with a unique role session name for yourself.

## Example usage

* NOTE: Each command and option has tab completion (where needed)
* You can use the identifier or name to refer to a CloudShell environment

### Listing CloudShell environments

```bash
$ csi ls
90356db8-8797-4d97-b776-2fb3696e0132  default                       RUNNING
d29340e9-d1a5-4509-964a-df67271410cf  csi-i-0441309a8e1338cd1-443   SUSPENDED  vpc-00235e1cd5f421ea3  subnet-09109a275b488cb8b
e8278021-e179-4e44-9e7d-6fedd64960f1  csi-rds                       SUSPENDED  vpc-00235e1cd5f421ea3  subnet-09109a275b488cb8b,subnet-0c8fb515762607bcc
```

### Creating a CloudShell environment

```bash
# Create a default CloudShell environment (no VPC)
$ csi create

# Create a VPC CloudShell environment in a specific subnet, using the default security group
$ csi create --name my-vpc-shell --subnets subnet-01234567890abcdef

# Create a VPC CloudShell environment in a specific subnet, specifying a security group
$ csi create --name my-vpc-shell --subnets subnet-01234567890abcdef --security-groups sg-01234567890abcdef
```

### Managing CloudShell environments

```bash
# Start an environment
$ csi start default
$ csi start 90356db8-8797-4d97-b776-2fb3696e0132

# Stop an environment
$ csi stop default

# Delete an environment
$ csi delete default
```

### Connecting to a CloudShell environment via Systems Manager (SSM)

```bash
$ csi ssm default

Starting session with SessionId: 1743751285551588149-un38ksdoyu7u7suz6li3vx53r4
~ $ whoami
cloudshell-user
```

### Executing commands on a CloudShell environment

```bash
$ csi execute default -c 'aws s3 ls'
```

### Uploading and Downloading files

```bash
$ csi upload default /tmp/data.sql /tmp/
$ csi upload default /tmp/data.sql /tmp/data.sql

$ csi download default /tmp/data.sql /tmp/
```

### Magic Genie

Genie magically creates and connects to a CloudShell environment with the correct network access to reach the resource you specify.

Under the hood, the genie command will:
1. look up your resource
2. find the associated ENI, and capture the VPC and subnets the resource lives in
3. attempt to find any security groups allowed to access the resource for the specified port and protocol
4. if none are found, it will check whether the CIDR range for any of the subnets captured earlier overlap in any whitelisted IP CIDR range rules for the specified port and protocol
5. if 3) and 4) fail - then the command will exit with an error
6. otherwise, genie will stand up a CloudShell in the appropriate subnet(s) and security groups, and attempt to add the default security group (if it exists)

Temporary genie environments can be created with `--tmp`

```bash
# Connect to an EC2 instance on port 22
$ csi genie --ec2 i-01234567890abcdef --port 22

# Connect to an RDS instance
$ csi genie --rds my-database-instance

# Connect to a specific IP and port
$ csi genie --ip 10.0.0.123 --port 3306

# Connect to a hostname and port (note this hostname must be externally resolvable)
$ csi genie --host internal-service.example.com --port 8080

# Create a temporary environment that will be deleted after use with --tmp
$ csi genie --ec2 i-01234567890abcdef --port 22 --tmp

# Create a CloudShell and output the ID to stdout
$ csi genie --ec2 i-01234567890abcdef --port 22 --output-id
```

## Roadmap

* [x] Use name of environment instead of IDs when issuing commands
* [x] Inject credentials
* [x] Upload files
* [x] Download files
* [x] genie: re-use existing environments if the VPC configuration is compatible
* [x] Genie mode for IP/EC2/RDS access
* [x] Temporary environments
* [x] Tab completion
* [ ] pub to pypi
* [x] Better tab completion (complete opts)
* [ ] Port tunneling (hard)
* [x] Output genie ID (do not connect)
