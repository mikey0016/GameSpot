"""Run a command on the remote server via SSH (password from env). Local helper only."""
import os
import sys

import paramiko


def main() -> None:
    host = os.environ["SSH_HOST"]
    user = os.environ["SSH_USER"]
    password = os.environ["SSH_PASS"]
    command = sys.argv[1] if len(sys.argv) > 1 else "echo ok"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=20)
    stdin, stdout, stderr = client.exec_command(command, timeout=300)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
