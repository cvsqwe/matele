# MAX Bot API schema

`schema.json` is the official OpenAPI description of the MAX Bot API, converted from YAML to JSON (content unchanged).

| | |
|---|---|
| Source | https://github.com/max-messenger/api-schema (`schema.yaml`) |
| Upstream commit | `1a4a502fab` (2026-09-18) |
| API version | 0.0.33 |
| License | Apache License 2.0 (as declared in the schema's `info.license`), https://www.apache.org/licenses/LICENSE-2.0 |

`src/matele/max/models.py` is generated from it. To update:

```bash
curl -sL https://raw.githubusercontent.com/max-messenger/api-schema/main/schema.yaml -o schema.yaml
python -c "import yaml, json; json.dump(yaml.safe_load(open('schema.yaml', encoding='utf-8')), open('schema/max/schema.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)"
python tools/gen_max_models.py
```

then update the commit and version above, run the tests and review the diff of `models.py`.
