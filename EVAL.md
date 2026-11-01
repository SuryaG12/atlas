# Atlas M5 evaluation

Retrieval numbers come from the exact M2 path: `retrieve(question, top_k=8, budget=6000, min_score=0.02)` with RRF fusion and top-8 packing. No embeddings were used.

**Narration: partial** — 1 narration request(s) failed; retrieval metrics are unaffected.

## Headline

| Metric | Result |
| --- | ---: |
| Retrieval recall@5 | 0.875 (42/48) |
| Refusal accuracy (M2 rule) | 0.417 (5/12) |
| Citation validity | 1.000 (39/39) |
| Faithfulness | 0.076 (50/660) |

The refusal number is weak: the M2 threshold accepts any question whose best fused score reaches 0.02, even when the matched code is unrelated. The refusal cases below name every false positive.

## Repositories

| Repo | Pinned version | Commit | Files | Symbols | Chunks | Edges |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `fastapi` | [0.119.1](https://github.com/fastapi/fastapi) | `864b569cf8453654fc3bc2c64108c0f644e2918c` | 2284 | 4760 | 4759 | 14215 |
| `flask` | [3.1.3](https://github.com/pallets/flask) | `22d924701a6ae2e4cd01e9a15bbaf3946094af65` | 225 | 1574 | 1544 | 3986 |
| `atlas` | [M4](https://github.com/SuryaG12/atlas) | `b534e9457edb0b0e3ef616d566d9821cb398aa80` | 33 | 341 | 341 | 2510 |

## Question set

| Repo | Questions | Answerable | Unanswerable |
| --- | ---: | ---: | ---: |
| `fastapi` | 20 | 16 | 4 |
| `flask` | 20 | 16 | 4 |
| `atlas` | 20 | 16 | 4 |
| **overall** | **60** | **48** | **12** |

## Metrics

Recall@5 is the fraction of answerable questions with an expected file or symbol in the top 5 retrieved chunks. Citation validity and faithfulness are narration-only. Refusal accuracy uses the M2 rule: `best_score < 0.02` and no exact symbol match.

| Scope | Recall@5 | Citation validity | Faithfulness | Refusal accuracy |
| --- | ---: | ---: | ---: | ---: |
| `fastapi` | 0.812 (13/16) | 1.000 (11/11) | 0.034 (8/238) | 0.500 (2/4) |
| `flask` | 0.875 (14/16) | 1.000 (9/9) | 0.095 (18/190) | 0.500 (2/4) |
| `atlas` | 0.938 (15/16) | 1.000 (19/19) | 0.103 (24/232) | 0.250 (1/4) |
| **overall** | 0.875 (42/48) | 1.000 (39/39) | 0.076 (50/660) | 0.417 (5/12) |

## Retrieval failures

6 of 48 answerable questions missed the top 5. These are the real failures, not hidden:

- `fastapi-06` (fastapi) — How does FastAPI classify a function parameter as a query, path, body, or dependency? — **no expected file or symbol in the top 5**. Expected files `fastapi/dependencies/utils.py`; symbols `analyze_param`.
  - Top 5: `fastapi/applications.py:1077-1128` FastAPI.setup; `fastapi/security/api_key.py:23-112` APIKeyQuery; `fastapi/applications.py:51-210` FastAPI; `fastapi/openapi/models.py:274-276` Parameter; `fastapi/security/api_key.py:115-200` APIKeyHeader
- `fastapi-09` (fastapi) — How does FastAPI validate and serialize the value returned by an endpoint? — **no expected file or symbol in the top 5**. Expected files `fastapi/routing.py`; symbols `serialize_response`.
  - Top 5: `fastapi/applications.py:1077-1128` FastAPI.setup; `fastapi/datastructures.py:149-153` UploadFile._validate; `fastapi/_compat/model_field.py:34-40` ModelField.validate; `fastapi/openapi/models.py:37-43` EmailStr._validate; `fastapi/dependencies/utils.py:733-748` _validate_value_with_model_field
- `fastapi-10` (fastapi) — Where is an HTTPException converted into a JSON error response? — **no expected file or symbol in the top 5**. Expected files `fastapi/exception_handlers.py`; symbols `http_exception_handler`.
  - Top 5: `fastapi/exceptions.py:37-65` HTTPException.__init__; `fastapi/exceptions.py:9-65` HTTPException; `tests/test_pydantic_v1_v2_mixed.py:32-37` NewItem; `tests/test_pydantic_v1_v2_mixed.py:95-104` handle_v1_item_to_v2_list; `tests/test_pydantic_v1_v2_mixed.py:28-29` NewSubItem
- `flask-02` (flask) — What runs before and after request hooks around a dispatched request? — **no expected file or symbol in the top 5**. Expected files `src/flask/app.py`; symbols `Flask.full_dispatch_request`.
  - Top 5: `tests/test_async.py:109-112` test_async_before_after_request.before; `tests/test_async.py:114-118` test_async_before_after_request.after; `tests/test_basic.py:853-855` test_before_after_request_order.before1; `tests/test_basic.py:857-859` test_before_after_request_order.before2; `tests/test_basic.py:861-864` test_before_after_request_order.after1
- `flask-14` (flask) — How does Flask load configuration from a Python file? — **no expected file or symbol in the top 5**. Expected files `src/flask/config.py`; symbols `Config.from_pyfile`.
  - Top 5: `src/flask/cli.py:493-512` _env_file_callback; `src/flask/cli.py:706-771` load_dotenv; `src/flask/cli.py:531-690` FlaskGroup; `tests/test_config.py:186-195` test_config_missing_file; `src/flask/json/__init__.py:108-135` load
- `atlas-04` (atlas) — How does Atlas choose a parser for a file based on its detected language? — **no expected file or symbol in the top 5**. Expected files `src/atlas/parse.py`; symbols `parse_file`.
  - Top 5: `src/atlas/parser_treesitter.py:30-40` _parser; `src/atlas/report.py:28-37` language_stats; `src/atlas/langs.py:105-111` detect_language; `src/atlas/walker.py:73-83` _shebang_language; `src/atlas/parser_treesitter.py:121-168` _Walker.walk

## Refusal cases

- `fastapi-17` (fastapi) — correct refusal — Where is the Kubernetes ingress sharding logic implemented?
  - best score 0.0164; exact symbol match: False; rule: best fused score 0.0164 < 0.0200 and no exact symbol match
- `fastapi-18` (fastapi) — **false positive** — Where is the Redis cluster failover handler implemented?
  - best score 0.0308; exact symbol match: False; rule: did not refuse (best score 0.0308 >= 0.02)
  - Top 5: `tests/test_exception_handlers.py:8-9` http_exception_handler; `docs_src/handling_errors/tutorial004.py:9-11` http_exception_handler; `tests/test_enforce_once_required_parameter.py:19-24` foo_handler; `docs_src/custom_request_and_route/tutorial001.py:18-26` GzipRoute; `fastapi/exception_handlers.py:11-17` http_exception_handler
- `fastapi-19` (fastapi) — correct refusal — Where is the gRPC streaming interceptor configured?
  - best score 0.0164; exact symbol match: False; rule: best fused score 0.0164 < 0.0200 and no exact symbol match
- `fastapi-20` (fastapi) — **false positive** — Where is the GraphQL subscription resolver defined?
  - best score 0.0460; exact symbol match: True; rule: did not refuse (best score 0.0460 >= 0.02; exact symbol match present)
  - Top 5: `scripts/notify_translations.py:239-248` get_graphql_translation_discussions; `scripts/notify_translations.py:251-261` get_graphql_translation_discussion_comments_edges; `scripts/contributors.py:148-153` get_graphql_pr_edges; `scripts/contributors.py:121-145` get_graphql_response; `scripts/sponsors.py:119-124` get_graphql_sponsor_edges
- `flask-17` (flask) — correct refusal — Where is the Kubernetes ingress sharding logic implemented?
  - best score 0.0164; exact symbol match: False; rule: best fused score 0.0164 < 0.0200 and no exact symbol match
- `flask-18` (flask) — **false positive** — Where is the S3 multipart upload retry policy implemented?
  - best score 0.0306; exact symbol match: False; rule: did not refuse (best score 0.0306 >= 0.02)
  - Top 5: `src/flask/wrappers.py:197-210` Request._load_form_data; `src/flask/debughelpers.py:81-104` attach_enctype_error_multidict; `src/flask/helpers.py:394-404` _prepare_send_file_kwargs; `src/flask/app.py:308-328` Flask.send_static_file; `src/flask/helpers.py:533-574` send_from_directory
- `flask-19` (flask) — **false positive** — Where is the Kafka consumer group rebalance handler implemented?
  - best score 0.0460; exact symbol match: True; rule: did not refuse (best score 0.0460 >= 0.02; exact symbol match present)
  - Top 5: `tests/test_cli.py:385-395` test_flaskgroup_nested; `src/flask/cli.py:429-437` AppGroup.group; `src/flask/cli.py:405-437` AppGroup; `src/flask/cli.py:413-427` AppGroup.command; `src/flask/blueprints.py:19-53` Blueprint.__init__
- `flask-20` (flask) — correct refusal — Where is the GraphQL subscription resolver defined?
  - best score 0.0000; exact symbol match: False; rule: no symbol or text matches
- `atlas-17` (atlas) — **false positive** — Where is the vector embedding index migration defined?
  - best score 0.0433; exact symbol match: True; rule: did not refuse (best score 0.0433 >= 0.02; exact symbol match present)
  - Top 5: `tests/test_m3.py:92-104` test_meta_tree_matches_index; `tests/test_m4.py:29-34` index; `src/atlas/web.py:88-93` _file_index; `tests/test_m4.py:663-670` test_cli_index_text_still_shows_the_new_counters; `tests/test_m4.py:452-456` test_diff_without_an_index_is_a_clean_error
- `atlas-18` (atlas) — **false positive** — Where is the hosted multi-tenant billing service implemented?
  - best score 0.0291; exact symbol match: False; rule: did not refuse (best score 0.0291 >= 0.02)
  - Top 5: `tests/test_m2.py:71-85` make_chunk; `tests/test_m2.py:234-242` test_validation_accepts_real_citation; `src/atlas/answer.py:113-155` validate_citations; `tests/test_m2.py:276-284` test_validation_keeps_good_and_strips_bad; `tests/test_m2.py:310-311` test_answer_flags_invalid_citation_from_model.narrator
- `atlas-19` (atlas) — correct refusal — Where is the Kubernetes ingress sharding logic implemented?
  - best score 0.0164; exact symbol match: False; rule: best fused score 0.0164 < 0.0200 and no exact symbol match
- `atlas-20` (atlas) — **false positive** — Where does Atlas negotiate WebSocket compression for browser clients?
  - best score 0.0323; exact symbol match: False; rule: did not refuse (best score 0.0323 >= 0.02)
  - Top 5: `src/atlas/cli.py:433-498` web_cmd; `tests/test_m3.py:272-276` test_cli_web_requires_an_index; `tests/test_m3.py:236-253` test_web_ask_matches_cli_ask; `tests/test_m3.py:263-269` test_cli_web_help_documents_flags; `tests/test_m4.py:577-601` test_web_reads_the_incremental_graph

## Paraphrase diagnostic

Questions that contain the expected symbol name are compared with questions that describe the same code in different words.

| Group | Questions | Recall@5 |
| --- | ---: | ---: |
| Exact code name in question | 5 | 1.000 (5/5) |
| Paraphrase | 43 | 0.860 (37/43) |

Gap (exact - paraphrase): **+0.140**. This is a material gap at the 0.10 threshold. No embeddings were added in M5.

Sample sizes are small (especially the exact-name group), so treat the gap as directional rather than a precise estimate.

## Ground truth

48 answerable questions were verified against the indexed files, symbols, and chunk ranges before scoring. Unverifiable questions would have aborted the run.

## Narration failures

- fastapi-01: could not reach ollama at http://127.0.0.1:11434: timed out
