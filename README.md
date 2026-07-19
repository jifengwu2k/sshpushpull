Dead simple, jargon-free Python tool to make a local TCP port available on a remote host or make a remote TCP port available locally, with auto-reconnect.

This is a Python reimplementation of [push-pull-port](https://github.com/jifengwu2k/push-pull-port) that uses `paramiko` instead of shelling out to `autossh`, making it cross-platform and self-contained.

## Motivation

**Modern work is mobile.** Whether you're at home, in a cafe, or on the move with 4G, you need secure, on-demand access to your devices and services - without wrestling with complex forwarding rules or VPNs.

`sshpushpull` lets you instantly "push" or "pull" any TCP port via SSH with human-friendly commands.

- Expose your device's SSH, VNC, or web apps at a moment's notice.
- Stop and start tunnels with a single command.
- Failures show up *immediately* - no silent, sneaky background errors.
- Auto-reconnects after transient network drops with exponential backoff.

**It's the Unix philosophy, everywhere:** portable, composable, and under your control.

## Prerequisites

- Python 2 or Python 3
- `paramiko` (installed automatically via `pip install sshpushpull`)
- SSH access to the remote host
- For unattended tunnels, set up SSH key-based authentication so you're not prompted for a password on every reconnect.

> **Note:** When using `push`, to make the pushed port accessible from other hosts, ensure the remote SSH server has `GatewayPorts clientspecified` set in `/etc/ssh/sshd_config`.

## Installation

```bash
pip install sshpushpull
```

## Usage

`sshpushpull` provides two subcommands:

- `push` — Make a local TCP port available on a remote host (equivalent to `ssh -R`)
- `pull` — Make a remote TCP port available locally (equivalent to `ssh -L`)

Both commands run in the foreground and auto-reconnect after transient network failures with exponential backoff (up to 10 seconds).

### Push: Expose a local port on a remote host

```
sshpushpull push \
    --local-port <local_port> \
    --remote-port <remote_port> \
    --username <user> \
    --host <host> \
    [--port <ssh_port>] \
    [--password <pwd> | --rsa-key <path> | --ed25519-key <path>] \
    [--localhost-only]
```

| Option | Description |
|--------|-------------|
| `--local-port` | Local TCP port to push from |
| `--remote-port` | Remote TCP port to push to |
| `--username` | SSH username on the remote host |
| `--host` | Remote SSH host name or address |
| `--port` | SSH server port (default: 22) |
| `--password` | Password for SSH authentication |
| `--rsa-key` | Path to RSA private key for SSH authentication |
| `--ed25519-key` | Path to Ed25519 private key for SSH authentication |
| `--localhost-only` | Open remote port on localhost only (default: open on all interfaces) |

**Examples:**

```bash
# Make local port 3000 available on dev.example.com:3001 using password auth
sshpushpull push --local-port 3000 --remote-port 3001 --username dev --host dev.example.com --password secret

# Using an Ed25519 key
sshpushpull push --local-port 3000 --remote-port 3001 --username dev --host dev.example.com --ed25519-key ~/.ssh/id_ed25519

# If SSH is running on port 2222
sshpushpull push --local-port 3000 --remote-port 3001 --username dev --host dev.example.com --port 2222 --rsa-key ~/.ssh/id_rsa

# Only allow access from the remote host's own localhost
sshpushpull push --local-port 3000 --remote-port 3001 --username dev --host dev.example.com --ed25519-key ~/.ssh/id_ed25519 --localhost-only
```

### Pull: Access a remote port locally

```
sshpushpull pull \
    --remote-port <remote_port> \
    --local-port <local_port> \
    --username <user> \
    --host <host> \
    [--port <ssh_port>] \
    [--password <pwd> | --rsa-key <path> | --ed25519-key <path>]
```

| Option | Description |
|--------|-------------|
| `--remote-port` | Remote TCP port to pull from |
| `--local-port` | Local TCP port to pull to |
| `--username` | SSH username on the remote host |
| `--host` | Remote SSH host name or address |
| `--port` | SSH server port (default: 22) |
| `--password` | Password for SSH authentication |
| `--rsa-key` | Path to RSA private key for SSH authentication |
| `--ed25519-key` | Path to Ed25519 private key for SSH authentication |

**Examples:**

```bash
# Access remote port 3306 (database) through local port 3307
sshpushpull pull --remote-port 3306 --local-port 3307 --username admin --host db.internal --password secret

# Using an Ed25519 key
sshpushpull pull --remote-port 3306 --local-port 3307 --username admin --host db.internal --ed25519-key ~/.ssh/id_ed25519

# If SSH is running on port 2222
sshpushpull pull --remote-port 3306 --local-port 3307 --username admin --host db.internal --port 2222 --rsa-key ~/.ssh/id_rsa
```

## Foreground Operation: Visibility Over Stealth

We intentionally run all tunnels in the **foreground**. This ensures:

- **Immediate Error Visibility:** Any connection issues, authentication failures, or port conflicts are clearly printed to your terminal, so you can respond and debug without guessing.
- **No Silent Failures:** By avoiding background daemons, you won't miss subtle (or catastrophic) tunnel dropouts that go unnoticed.
- **Stopping the tunnel:** Simply press `Ctrl-C` to stop the tunnel. You can also close the terminal window/tab to end it.

> Tip: If you ever want to run the tunnel in the background, you can use a terminal multiplexer like `tmux` to keep tunnels running while detached.

## Auto-Reconnect

Both `push` and `pull` automatically reconnect after transient network failures. The reconnect strategy uses exponential backoff:

1. First retry: 1 second
2. Second retry: 2 seconds
3. Third retry: 4 seconds
4. ...up to a maximum of 10 seconds

After a successful reconnection, the backoff resets to 1 second.

## Why We Use Push/Pull Instead of Forward/Reverse

### The Problem with Traditional Terms

The standard SSH port forwarding terms (`local forwarding` vs `remote forwarding`) are notoriously confusing because:

1. **Perspective Dependence**  
   The "remote" and "local" labels depend on which machine initiates the SSH connection, not the actual service exposure direction users care about.

2. **Cognitive Mismatch**  
   When developers want to:
   - **Expose a local service** remotely, they must remember this is called "remote forwarding" (`-R`)
   - **Access a remote service** locally, this is called "local forwarding" (`-L`)

3. **Implementation-First Naming**  
   The terms describe SSH's technical implementation rather than user intent.

### Our Push/Pull Metaphor

We intentionally avoid `forward/reverse` terminology in favor of intuitive action verbs:

| User Goal                                 | Traditional Term    | Our Term | SSH Option |
|--------------------------------------------|---------------------|----------|------------|
| Make local service available on remote host| Remote Forwarding   | Push     | `ssh -R`   |
| Access remote service through local port   | Local Forwarding    | Pull     | `ssh -L`   |

### Key Advantages

1. **Intent-Oriented**  
   - `push`: "I want to make this local port available there"  
   - `pull`: "I want to access that remote port here"

2. **Directionally Clear**  
   Eliminates ambiguity about "whose local/remote" we're referring to.

3. **Cloud-Native Alignment**  
   Matches modern service mesh concepts (ingress/egress) better than SSH's 1990s perspective.

## Contributing

Contributions are welcome! Please submit pull requests or open issues on the GitHub repository.

## License

This project is licensed under the [MIT License](LICENSE).
