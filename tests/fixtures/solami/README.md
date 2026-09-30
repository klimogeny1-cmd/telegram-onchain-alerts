# Solami fixtures

Everything in this folder is **synthetic** and used only by the offline test suite, except
`live_frames_2026-09-30.jsonl` - real frames of the first live run, one of each type, with the
`api_key` that Solami puts into metadata `image_url` removed:

- `blur_frames.jsonl` - hand-built Blur WebSocket frames following the event shapes documented at
  solami.dev/docs/blur (read 2026-09-26). Made-up base58 addresses. Also used by the offline demo
  (`examples/offline-demo.conf`).
- `rpc_*.json` - standard Solana JSON-RPC response shapes (`getAccountInfo` jsonParsed for SPL and
  Token-2022 mints, `getTokenLargestAccounts[V2]`, `getMultipleAccounts`, `getSlot`) and the error
  bodies Solami documents at solami.dev/docs/errors.
- `data_api_metadata.json` - assumed shape of `GET /data/token/metadata` (assumption D1 in README).

Once a real key exists, `python3 main.py --check-live 60 --record tests/fixtures/solami/recorded.jsonl`
captures real frames; see README "Assumptions to verify with a real key".
