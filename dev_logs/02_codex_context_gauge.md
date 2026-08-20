# (2) codex context 게이지 누락 — 구현 로그

## 원인 (plan §3.1 / owner 실측 — 재조사 없이 그대로 채택)
`_parse_codex_attempt_tail()`가 512 KiB 꼬리만 읽어, 파일 첫 줄에만 있는 `thread.started`의
`thread_id`를 로그가 512 KiB를 넘는 순간 영구히 놓친다. `thread_id=None` → rollout 조인 스킵 →
ctx/window/active 전부 `None`.

## 수정
`tools/fleet/collectors/dispatch.py`:
- `_codex_attempt_thread_ids(lines)` 헬퍼 신설 — `thread.started` 행에서 thread_id 집합 추출
  (기존 메인 루프의 해당 분기 로직을 그대로 재사용 가능하게 뽑아냄, 새 개념 아님).
- `_parse_codex_attempt_tail(path)`:
  - `st.st_size <= head_bytes + tail_bytes`(64 KiB + 512 KiB)이면 **파일 전체를 한 번만 읽는다**
    (기존 소형 파일 단일 읽기 동작 그대로 유지·확장).
  - 그보다 크면 머리 64 KiB(`_CLAUDE_SUPERVISOR_HEAD_BYTES`, 기존 상수 재사용 — 새 상수 도입 없음)와
    꼬리 512 KiB(`_CLAUDE_STREAM_TAIL_BYTES`) **두 조각만** 읽는다. 읽는 바이트 수는 파일 크기와
    무관한 상수 상한 이하로 고정된다.
  - 경계에서 잘린 줄은 기존과 동일하게 버린다 — 꼬리는 `lines[1:]`(seek 지점의 부분 줄), 머리는
    `head_lines[:-1]`(head_bytes 지점에서 잘렸을 수 있는 마지막 줄).
  - `thread_ids`는 머리+꼬리 **합집합**(`thread_ids = set(head_thread_ids)`로 시드한 뒤 메인 루프가
    꼬리 쪽 `thread.started`를 더한다) — `thread_ambiguity` 의미론(2개 이상이면 귀속 포기) 불변.
  - 캐시 키 `(mtime_ns, size)` 불변, `latest_usage`/`open_commands`는 여전히 꼬리(현재 상태)에서만
    계산 — plan이 바꾸라고 한 적 없다.
  - 값 합성 없음: 분모는 여전히 `_enrich_codex_attempt_session`의 rollout 조인에서만 온다
    (이 함수는 손대지 않았다). 실패 시 여전히 `ctx_pct=None` → render가 빈 게이지 + `—`.

## 재현 검증 (checklist B9)
owner 실측 evidence의 depth-1 owner 로그(`artifact-knowledge-index-w0.att-7da8….codex.jsonl`,
1,350,282 B)로 수정 후 직접 재실행:

```
size 1350282
{'token_usage': None, 'thread_id': '01a01e12-dd3a-7240-9fcd-4c30d6d18722',
 'thread_ambiguity': False, 'exec_tool': {'name': 'zsh'}}
```

수정 전(`codex-ctx-repro.txt`)에는 `parsed thread_id=None ambiguity=False` → `RESULT ctx_pct=None
window=None active=None`. 수정 후 `thread_id`가 채워졌으므로 `_enrich_codex_attempt_session`의
rollout 조인이 더 이상 스킵되지 않는다 — 실제 `ctx_pct`/`window`/`active` 값은 해당 attempt의
rollout 파일 상태(`~/.codex/` 아래 실제 세션이 살아있는지)에 달려 있어 이 노드에서는 `thread_id`
복구까지만 직접 확인했다. rollout 조인 자체는 별도 함수(`_codex_attempt_rollout`)이며 이번
변경의 대상이 아니다.

## 테스트 (checklist B1~B9)
새 파일 `tools/fleet/tests/test_f82_codex_attempt_head.py` — 5개:
1. `test_thread_id_survives_oversize_log` — 1.5 MB 합성 로그, 첫 줄만 `thread.started`.
2. `test_read_bytes_do_not_scale_with_file_size` — `open`을 계측해 1.5 MB/6 MB 로그 모두
   `head_bytes+tail_bytes` 상한 이하로 읽힘을 단언.
3. `test_ambiguity_preserved_across_head_and_tail` — 머리·꼬리에 서로 다른 thread_id →
   `thread_ambiguity=True`, `thread_id=None`.
4. `test_malformed_first_line` — 첫 줄이 깨진 JSON → 예외 없이 `thread_id=None`.
5. `test_small_log_single_read` — 임계 이하 파일은 `open` 1회 호출만 발생.

픽스처는 `tempfile.TemporaryDirectory()`로 테스트 시점 생성, 트리에 커밋하지 않음(B7).
각 테스트에 "임계 아래 픽스처는 이 회귀를 못 잡는다" 취지의 docstring/주석을 남겼다(B8) —
모듈 docstring과 `test_thread_id_survives_oversize_log`의 주석 참고.

실행 결과(anaconda pytest, stdlib unittest 양쪽 확인):
```
$ /home/Uihyeop/anaconda3/bin/python -m pytest tools/fleet/tests/test_f82_codex_attempt_head.py -q
.....                                                                    [100%]
5 passed in 1.46s
```
