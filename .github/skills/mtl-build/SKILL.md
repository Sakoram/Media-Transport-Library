---
name: mtl-build
description: 'Build MTL library, format code, and verify compilation. Use when asked to build, verify changes, check formatting, or prepare code for commit.'
---

# Build & Verify MTL

## Build Modes

Run from the repository root:

| Command | Mode | Use when |
|---------|------|----------|
| `./build.sh` | Release | Default. Optimized build for testing and deployment |
| `./build.sh debug` | Debug + ASAN | Debugging with AddressSanitizer (`-O0 -g`). Slower but catches memory bugs |
| `./build.sh debugonly` | Debug only | Debug symbols without ASAN. Faster than `debug` for general development |
| `./build.sh unit` | Unit gtest | Configures `build_unit/` with `-Denable_unit_tests=true`, builds it, and runs `build_unit/tests/unit/UnitTest` (one suite after a build: `./build_unit/tests/unit/UnitTest --gtest_filter='St40*'`). Combine with `debug` (e.g. `./build.sh debug unit`) to run under ASan via `LD_PRELOAD` |

## Format Code

```bash
./format-coding.sh --files a.c b.h   # PREFERRED: fix only the files you edited
./checkpatch.sh --files a.c b.h      # verify them (./checkpatch.sh alone checks the tree, as CI does)
./format-coding.sh                   # the whole tree; only for a sweeping change
```

- `./format-coding.sh --check` previews changes (non-mutating check/diff
  mode for every tool, exits 1 if anything would change) instead of writing
  to disk. Always use this to inspect a formatting change's blast radius
  before running the real, mutating command on a working tree that has
  uncommitted work in it.
- Nothing to install but `pre-commit` (`./checkpatch.sh --bootstrap`):
  `.pre-commit-config.yaml` pins every tool, clang-format 22.1.8 from
  mirrors-clang-format among them, so the host's `clang-format` does not
  matter. `.clang-format` is a real file at the root, not a symlink
  ([coding standard](../../../doc/coding_standard.md) §2.1)
- **CI rejects improperly formatted code** — always run before committing
- Formats C/C++, Python (`isort` then `black`), Markdown, and Shell scripts;
  after a run, `git diff --name-only` shows what it touched

## Verification Checklist

After building, verify:
1. Build completes without errors
2. Key artifacts exist:
   - `build/lib/libmtl.so` — shared library
   - `build/tests/KahawaiTest` — integration test binary
   - `build/app/RxTxApp` — reference application
3. Run `./format-coding.sh` and check for any formatting changes

## Common Build Errors

| Error | Fix |
|-------|-----|
| Missing `meson` / `ninja` | `sudo apt install meson ninja-build` |
| Missing `libnuma-dev` | `sudo apt install libnuma-dev` |
| Missing `libjson-c-dev` | `sudo apt install libjson-c-dev` |
| Missing `libpcap-dev` | `sudo apt install libpcap-dev` |
| Missing `libgtest-dev` | `sudo apt install libgtest-dev` |
| Missing `libssl-dev` | `sudo apt install libssl-dev` |
| Missing `systemtap-sdt-dev` | `sudo apt install systemtap-sdt-dev` (for USDT probes) |
| DPDK not found / wrong version | Build DPDK first: see [build docs](../../doc/build.md) |
| Stale build directory | `rm -rf build && ./build.sh` or use MCP tool `mtl_clean_rebuild` |
| Permission errors in build/ | `sudo rm -rf build && ./build.sh` |
| Build exits early, `jobserver` warnings | A user-local `ninja` (for example `~/.local/bin/ninja`) ahead of `/usr/bin/ninja` on `PATH`: call `/usr/bin/ninja -C build_unit` explicitly |
| Crash or segfault in a test that a clean tree does not reproduce | Stale or root-owned `build*/.ninja_deps` from an earlier `sudo` build: `sudo rm -rf build_unit && ./build.sh unit`, then re-check before trusting the crash |
