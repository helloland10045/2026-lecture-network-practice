# Week 3 observation

## Task 1 · 반복적 리졸버

- 루트 서버는 `kr.` 아래의 이름을 하나도 모른다. 각 TLD를 누가 맡는지(위임)만 알기 때문에 주소 대신 referral(answer 0개, authority에 NS, additional에 glue A)을 돌려준다. 모든 이름을 한곳에 두지 않아서 관리와 확장이 나뉜다.
- glue 없이 NS 이름만 온 위임은, 그 NS 이름을 루트부터 다시 걸어서(`_walk` 재귀) 주소를 얻은 뒤 원래 질문을 이어가도록 구현했다. 이름이 자기 zone 안에 있고 glue도 없으면 순환이라 `_stack`으로 막고 다음 NS를 시도한다. 다만 `www.korea.ac.kr`은 위임마다 glue가 같이 와서 실제 glue 없는 조회는 **0회**였고, 이 경로는 가짜 DNS 계층으로만 시험했다 (실제 도메인에서는 아직 마주치지 못함).
- `www.korea.ac.kr` 한 이름에 물어본 서버는 3대(루트 198.41.0.4 → 위임을 한 번 더 준 서버 210.101.61.1 → 답을 준 163.152.1.1)이고, 노트북이 평소 리졸버에 하는 질문은 1개다. 나머지는 리졸버가 대신 걷고 캐시한다. `--verify`는 5/5 통과했다 (홉 수: dns.google 3, wikipedia 6, stanford 6, microsoft 10). dns.google와 stanford는 dig가 주소를 2개 줬고 내 리졸버는 그중 하나를 골랐다. CDN 이름 `www.microsoft.com`은 이번에는 dig와 같은 주소가 나왔고, 홉이 많은 건 CNAME을 따라 걷기를 다시 시작했기 때문으로 보인다 (`-v`로는 확인하지 않음).

## Task 2 · 캡처와 steering

- 시간이 부족해서 Wireshark 캡처(Part A)와 두 네트워크 측정(Part B)을 하지 못했다. 그래서 `out/dns.pcapng`, `out/chains.json`, `out/report.md`는 없다. 위임 응답과 답변 응답의 차이는 캡처가 아니라 리졸버의 `-v` 출력(referral / answer)으로만 봤다.
- `task2_steering.py`의 `--collect`와 `--report`는 작성했지만 가짜 `dig` 데이터로만 시험했고 실제 측정은 돌리지 못했다. 규칙은 "마지막 CNAME 홉의 등록 도메인이 사이트와 다르면 제3자"로 정했다. 이 규칙이 틀릴 것으로 예상하는 경우는 두 가지다: 같은 조직의 다른 도메인(wikipedia.org → wikimedia.org), CNAME 없이 anycast CDN 뒤에 있는 사이트. 실제 데이터로는 확인하지 못했다.
- steering 수치는 측정하지 못해서 (b) 주장을 지지하는지 말할 수 없다.

## Task 3 · 캐시

- 베이스라인의 문제는 뿌리가 같다. `FIXED_LIFETIME = 60`이 upstream이 준 TTL을 버리고 모든 레코드를 60초로 고정한다. 성능 문제: TTL 3600~86400초인 레코드(korea, stanford, dns.google, root)를 60초마다 다시 물어 쓸데없는 upstream 145회. 정확성 문제: TTL 20·30초인 microsoft·cnn을 60초 들고 있다가 만료된 답 266개를 내줬다.
- 하한은 **275회**이고 내 캐시도 275회다. 정확한 캐시는 만료된 레코드를 줄 수 없으므로(R3) TTL 창이 닫힌 뒤 도착한 첫 질문은 반드시 upstream으로 간다. 최선은 "첫 질문에 한 번 가져오고, 창이 닫힌 뒤 처음 오는 질문에만 다시 가는 것"이고 미리 가져오면 오히려 늘어난다. 이 값은 질의 도착 시각과 TTL이 정하는 것이지 자료구조의 영리함이 정하는 게 아니다 (이름별 합: 118+76+37+23+11+6+1+1+1+1).
- 가장 못 다루는 레코드는 `www.microsoft.com`(TTL 20초): 322번 질의 중 189번(59%)이 만료된 답이었다. 이 레코드는 베이스라인 upstream이 52회로 하한 118회보다 적은데, 그 "절약"은 만료된 답을 내주고 얻은 것이다.
