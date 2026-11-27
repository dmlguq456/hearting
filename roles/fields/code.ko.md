---
field: code
makes: working software
---
# code

개발(code)은 돌아가는 소프트웨어를 만드는 일이다.

## 기본 (빼지 않는다)

- 나아지는 변경이면 승인한다. — 코드의 건강을 분명히 나아지게 하는 변경이면 완벽하지 않아도 승인한다. [1–4]
- 작게 바꾼다. — 변경이 작을수록 리뷰가 빠르고 꼼꼼하며 버그가 준다. [1–4]
- 시험은 변경과 함께 낸다. — 변경에 대한 시험은 그 코드와 같은 변경에 담아 낸다. [1–4]
- 사후 검토는 탓하지 않는다. — 사람이 아니라 시스템과 과정을 고친다. [1–4]

## 정하지 않는 것

사람과 팀마다 다른 자리라서 이 글이 열어 둔다: 일의 차례, 도구, 리뷰와 시험의 깊이, 설계 문서를 쓸 변경의 크기, 결정을 따로 모아 둘지, 얼마나 자주 메인 줄기에 합칠지, 배포하는 방식, 팀이 "됐다"로 치는 기준.

## 다른 갈래와의 경계

- 만든 것을 합의된 시안과 맞대어 보는 일은 여기의 리뷰·시험과 겹치지만, 시안 자체는 `design`의 일이다.
- 모델이나 실험을 재고 그 결과를 해석하는 일은 `lab`의 일이다. 개발은 그것을 돌리는 소프트웨어다.
- 논문이나 보고서의 글은 코드에 관한 것이어도 `docs`의 일이다.

## 출처

줄 뒤의 번호는 분야 조사가 이 줄들을 묶어서 댄 출처다. 두 조사가 따로 확인했고 한쪽이 원문을 읽은 줄만 넣었다. "사후 검토"는 장애나 실패 뒤의 검토를 뜻한다. 출처의 사후 검토(postmortem)가 그 뜻이다.

1. Google Engineering Practices, The Standard of Code Review — https://google.github.io/eng-practices/review/reviewer/standard.html (조사 카드 S1, V1)
2. Google Engineering Practices, Small CLs — https://google.github.io/eng-practices/review/developer/small-cls.html (카드 S2)
3. Google SRE Book, Postmortem Culture — https://sre.google/sre-book/postmortem-culture/ (카드 S3, V3)
4. DORA capabilities (작은 묶음으로 일하기, 변경 승인 간소화) — https://dora.dev/capabilities/ (카드 S5, V2)

## 고치려면

틀린 곳이나 빠진 곳은 하팅 저장소의 이슈나 PR로 알려 주세요. 이 분야에서 일하는 분이면 누구나 좋습니다. 채워야 할 양식도, 따로 만든 창구도 없습니다.
