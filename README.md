# Loom CLI

Sign in and run the **Loom** agent from your terminal — the same account, keys,
and engines as the web app. Works like `claude`: `loom login`, then `loom`.

## Install

```bash
# curl
curl -fsSL https://loom.mbd.xyz/install.sh | sh

# Homebrew
brew install ZKAI-Network/loom/loom

# npm
npm install -g @embed-ai/loom
```

## Use

```bash
loom login                     # sign in (opens your browser for SSO)
loom -p "your question"        # one-shot
loom                           # interactive session
loom whoami                    # show who you're signed in as
loom sessions                  # list recent sessions
loom --server http://localhost:6767 …   # target a Loom server on your machine
```

Server precedence: `--server <url>` > `LOOM_SERVER` > your last login > `https://loom.mbd.xyz`.
