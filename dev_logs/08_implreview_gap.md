# 08 — impl-review blocking finding 1건 close-out

`_internal/dev_reviews/phase_review.md`의 `verdict: BLOCK` 재현을 그대로 닫았다. 범위는
이 1건뿐이며, 이미 커밋된 5개 커밋은 손대지 않았다.

## 결함

`tools/fleet/render.py`의 depth-1·depth-2 분류 elif 사다리 둘 다, `_parent_edge_sid` /
`_parent_edge_promoted_orphan` ledger를 확인하기 **전에** `j.parent_sid in shown_sids`
직접 판정 분기를 먼저 타고 있었다. `--all`이 dead 부모를 `shown_sids`에 넣으면, collector가
이미 `_parent_edge_promoted_orphan=True`로 확정한 행도 그 dead 부모 카드 안에 nest되고
`(orphan)` 마커가 사라졌다 — plan.md `_SHOW_ALL` 비간섭 규칙 위반.

## 수정 전 재현 확인 (필수 절차)

수정 전 `render.py`를 `git stash`로 복원한 뒤 새로 작성한 두 테스트를 돌려 실제로
실패하는 것을 먼저 확인했다:

```
test_show_all_does_not_override_promoted_orphan_depth1 ... FAIL
test_show_all_does_not_override_promoted_orphan_depth2 ... FAIL
AssertionError: '(orphan)' not found in '... dead-parent ... dead-child ... (nested, no orphan marker) ...'
```

이어서 `git stash pop`으로 수정을 복원하고 같은 두 테스트가 통과하는 것을 확인했다.

## 수정

`render.py`의 두 elif 사다리(depth-2: 옛 `:4949-4962`, depth-1: 옛 `:4963-4970`) 모두를
ledger-first 순서로 재배치했다:

1. `_parent_edge_promoted_orphan`이 True → `shown_sids`와 무관하게 즉시 orphan.
2. `_parent_edge_sid`가 채워져 있으면 → 그 sid로 귀속. `shown_sids`에 있으면 `children`,
   없으면(grace 유지 중) `grace_jobs`.
3. 위 두 ledger 필드가 모두 비어 있는 행(= collector가 `resolve_parent_edges`를 이 행에
   대해 아직 돌리지 않은 경우)에서만 기존 `j.parent_sid in shown_sids` fallback을 그대로 유지.
4. `j.parent_sid in shown_sids`는 이제 오직 "ledger가 확정한 sid를 `children`과
   `grace_jobs` 중 어디에 넣을지"에만 쓰이고, orphan/parent 귀속 판정 근거로는 쓰이지 않는다.

`_DETACHED_FINISHED_LIVENESS` 분기(depth-2 사다리의 `:4940-4948`, 자손 접힘 표시 정책)는
귀속 판정이 아니므로 그대로 두었다.

## 테스트

`tools/fleet/tests/test_f80_parent_edge_orphan.py`:

- `test_show_all_does_not_override_promoted_orphan_depth1` (신규) — dead 부모 +
  `_SHOW_ALL=True` + ledger promoted orphan → `(orphan)` 마커, dead 부모 카드에 nest되지
  않음. 수정 전 코드에서 FAIL 확인 완료(위 절차 참조).
- `test_show_all_does_not_override_promoted_orphan_depth2` (신규) — 동일 시나리오의
  depth-2(`parent_slug`/`dispatch_depth=2`) 경로 버전. 역시 수정 전 FAIL 확인.
- `test_all_toggle_does_not_change_ledger_confirmation_through_classification` (신규) —
  기존 C10c 테스트(`session_parent_visible()`만 직접 호출)가 render의 분류 루프를 전혀
  타지 않는다는 gap을 메움: `resolve_parent_edges` → `_build_lines`까지 실제로 돌려서
  `--all` on/off 양쪽에서 grace-held 확정 edge가 orphan으로 재판정되지 않음을 확인.
- 기존 `test_confirmed_visible_parent_still_nests_normally` / 정상 confirmed nesting
  회귀 테스트는 그대로 통과.

## 검증

- `python3 -m unittest discover -s tools/fleet/tests -p "test_*.py" -t .` → **1498 passed**
  (직전 기준선 1495 + 신규 3개), 새 실패 0건.
- `rsync -a --delete --exclude='__pycache__' tools/fleet/ adapters/claude/tools/fleet/`로
  어댑터 미러 재동기(`test_mirror_matches_canonical` 통과 확인).
- CI 5스텝(`.github/workflows/checks.yml:28-41`) 전부 rc=0:
  `tools/generate.py --check`, `tools/render-landing.py --check`,
  `tools/check-adaptation-boundary.sh`, `tools/check-model-config.py`,
  `tools/check-unit-config.py`.
