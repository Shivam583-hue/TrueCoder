# TrueCoder execution image

Build and lock the sandbox image.
The runtime never pulls at command time, so the image must exist locally before the container backend reports itself available.

## Build

```sh
docker build -t truecoder-exec:1 container/
```

## Lock

Record the immutable content digest that launch pins against.

```sh
docker images --no-trunc --format '{{.ID}}' truecoder-exec:1
```

Write that value into both `reference` and `digest` in `container/image.lock`.
A locally built image has no registry manifest digest, so its content ID is the pinned identity.

## Verify

```sh
docker run --rm --network none truecoder-exec:1 --version
docker run --rm --network none truecoder-exec:1 python3 -c "import os; print(os.getuid(), os.getgid(), os.getcwd())"
docker run --rm --network none --read-only --cap-drop ALL truecoder-exec:1 sh -c 'echo x > /etc/probe'
```

The first command prints the entrypoint protocol version.
The second prints `65532 65532 /workspace`.
The third must fail with a read-only filesystem error.

## Rebuild policy

Rebuilding produces a new content digest.
Update `container/image.lock` in the same commit, because discovery refuses an image whose digest does not match the lock.
