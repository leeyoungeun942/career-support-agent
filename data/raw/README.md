# data/raw

이 폴더에는 채용공고 원문 데이터를 넣는다. 내용은 git에 올라가지 않는다(`.gitignore`).
파일은 팀 Drive 폴더 `career-support-agent-data`에서 받아 이 폴더에 넣는다.

- wanted_jds.json: 원티드 공고 60건
- eval_cases.json: 항목 분리본과 정답(개발셋·확인셋)

이 폴더의 파일을 다른 곳으로 옮겨 커밋하지 않는다.

샘플 공고 6건의 원문(주요 업무)은 `data/sample-jobs/gate_sample_cases.json`에 둔다. 이 파일도 커밋하지 않는다.
원문이 필요한 게이트 테스트(`tests/test_job_gate_sample.py`)는 이 파일이 없으면 건너뛴다.
