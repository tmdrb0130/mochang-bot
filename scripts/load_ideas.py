"""부하 테스트용 아이디어 풀 (2026-09-07) — 한국인 60 · 외국인 20.

실사용 초안 82건의 주제 분포를 보고 **같은 결로 새로 썼다**(학생이 쓴 문장을 그대로 옮기지 않는다 — 테스트 고정물에
남의 입력을 박아 두지 않기 위해). 실제 분포: 특수교육·장애 지원 약 30%, 캠퍼스·대학생 25%, 생활·소비 서비스 45%.

왜 60개인가: 조사 캐시가 `sha1(track|idea|question_id)` 로 걸리고 수명이 7일이다(`config.yaml research.cache_ttl_seconds`).
아이디어가 겹치면 검색·본문 추출·사실 추출이 **파일 읽기로 끝나** 부하가 사라지고, "40명이 버티나" 를 물었는데
훨씬 가벼운 것을 재게 된다. 사용자마다 다른 아이디어 + `unique_tail()` 로 회차마다 다른 꼬리를 붙여 캐시를 비껴간다.

주제를 넓게 잡은 이유는 검색어가 겹치지 않게 하기 위해서다 — 같은 주제만 60개면 조사 캐시가 아니라
**검색 결과 캐시**(`DiskCache`, 검색어 단위)에 걸린다.
"""
from __future__ import annotations

import random

# ── 한국인 60 (name, idea, capability) ──
KR_IDEAS: list[tuple[str, str, str]] = [
    # ── 특수교육·장애 지원 (18) — 실사용에서 가장 많은 갈래 ──
    ("보조기기 대여", "특수학급 담임을 하면서 학생마다 필요한 보조기기가 달라 학교 예산으로 다 갖추기 어렵다는 걸 느꼈다. 지역 내 학교들이 쓰지 않는 보조기기를 학기 단위로 서로 빌려주는 관리 시스템을 만들고 싶다.", "특수교육과 4학년, 특수학교 교육실습 8주"),
    ("의사소통 그림판", "말로 표현이 어려운 학생이 급식실이나 보건실에서 원하는 것을 전하지 못해 곤란해하는 걸 자주 봤다. 상황별 그림 카드를 태블릿에서 두 번만 눌러 문장으로 만들어 주는 앱을 만들려 한다.", "특수교육과 3학년, 보완대체의사소통 수업 이수"),
    ("등하교 인계 확인", "특수학교 통학버스는 학생이 제자리에 앉았는지, 보호자에게 제대로 인계됐는지를 기사와 실무사가 종이에 적는다. 좌석 태그와 보호자 확인을 앱 하나로 묶어 기록을 남기는 서비스를 만들고 싶다.", "특수학교 방과후 강사 1년, 안드로이드 앱 개발 학습 중"),
    ("행동 기록 공유", "학생의 돌발 행동은 담임이 바뀌면 처음부터 다시 파악해야 한다. 언제 어떤 자극에서 어떤 행동이 나왔는지를 짧게 기록해 다음 교사에게 넘기는 도구를 만들려 한다.", "특수교육과 4학년, 행동중재 실습 참여"),
    ("감각 안정 키트", "소음이나 조명 변화에 예민한 학생이 교실을 벗어나는 일이 잦은데, 진정에 쓰는 도구가 교사 개인 물건이라 학교마다 제각각이다. 학생별로 어떤 자극에 무엇이 효과 있었는지 기록하고 키트를 구성해 주는 서비스.", "특수교육과 3학년, 감각통합 관련 봉사 2년"),
    ("체육 목표 쪼개기", "특수체육 수업에서 '줄넘기 한 번 넘기' 같은 목표를 학생마다 몇 단계로 쪼개야 하는데 교사 경험에 의존한다. 학생 수준을 입력하면 단계별 연습 과제와 성공 기준을 제안해 주는 도구를 만들고 싶다.", "특수체육교육과 3학년, 장애인 생활체육 보조 1년"),
    ("휠체어 접근 지도", "전동휠체어를 쓰는 지인과 다니면서 경사로가 있다고 표시된 곳도 실제로는 문턱이 있어 못 들어가는 경우가 많았다. 이용자들이 직접 사진으로 확인해 쌓는 접근성 지도를 만들려 한다.", "재활복지학과 2학년, 장애인 활동지원사 자격 준비"),
    ("복약·식사 알림", "특수학급에서 학생별 복약 시간과 식사 시 주의사항(잘게 자르기, 점도 조절 등)을 교사와 실무사가 각각 메모해 놓쳐 위험한 상황이 생긴다. 학생 카드 하나에 모아 시간이 되면 알려 주는 앱.", "특수교육과 4학년, 특수학급 보조 실습"),
    ("운동 안전 범위", "특수체육 참여자마다 무리 없는 운동 범위가 다른데, 기존 운동기구는 그걸 알려주지 않아 다치는 일이 있다. 기구에 붙이는 센서로 각도와 반복 수를 재고 안전 범위를 넘으면 신호를 주는 장치를 만들고 싶다.", "특수체육교육과 4학년, 헬스 트레이너 자격증"),
    ("재난 대피 훈련", "재난 대피 훈련은 일반 학생 기준으로 짜여 있어 이동이 느리거나 소리에 예민한 학생은 그대로 따라가기 어렵다. 학교 도면과 학생 특성을 넣으면 반별 대피 순서를 만들어 주는 도구.", "특수교육과 3학년, 학교 안전 담당 봉사"),
    ("자립생활 체크", "졸업을 앞둔 학생이 혼자 버스를 타거나 물건을 사는 연습을 하는데 어디까지 되는지 기록이 남지 않는다. 항목별로 도움 정도를 체크해 학기별 변화를 보여 주는 앱.", "특수교육과 4학년, 전환교육 세미나 참여"),
    ("수어 학습 짝꿍", "농학생과 같은 반이 된 학생들이 기본 수어를 배우고 싶어도 짧게 배울 곳이 없다. 하루 3분씩 교실에서 함께 익히는 수어 학습 콘텐츠를 만들려 한다.", "특수교육과 2학년, 수어 동아리 2년"),
    ("놀이터 접근성", "동네 놀이터에 휠체어로 갈 수 있는 기구가 거의 없어 형제만 놀고 아이는 지켜보는 장면을 자주 봤다. 통합놀이터 위치와 실제 이용 가능 여부를 모아 알려주는 서비스.", "사회복지학과 3학년, 지역아동센터 봉사 2년"),
    ("교실 소음 알림", "청각 과민이 있는 학생은 교실 소음이 일정 수준을 넘으면 힘들어하는데 교사는 그걸 수치로 알기 어렵다. 교실 소음을 재서 기준을 넘으면 조용히 알려 주고 기록을 남기는 장치.", "특수교육과 3학년, 아두이노 기초"),
    ("치료실 예약", "지역 언어치료실과 감각통합치료실은 대기가 길고 취소분 정보가 전화로만 돈다. 취소 자리를 실시간으로 알려 주고 바로 예약하는 서비스를 만들고 싶다.", "언어재활과 3학년, 치료실 실습 6개월"),
    ("보호자 연계장", "가정에서 시도한 지도 방법과 학교에서 한 방법이 서로 공유되지 않아 학생이 혼란스러워한다. 교사와 보호자가 짧은 메모와 사진으로 그날의 방법을 주고받는 연계장을 만들려 한다.", "특수교육과 4학년, 학부모 상담 실습"),
    ("직업체험 매칭", "특수학교 고등부 학생이 지역에서 직업 체험할 곳을 찾기 어렵고, 사업장도 어떻게 맞이해야 할지 몰라 부담스러워한다. 체험 가능한 사업장과 준비 안내를 함께 제공하는 매칭 서비스.", "직업재활학과 3학년, 보호작업장 실습"),
    ("교구 3D 도면", "학생에게 맞는 교구가 시중에 없어 교사가 직접 만드는데, 만든 방법이 개인 파일로만 남는다. 3D 프린터 도면과 제작 방법을 교사끼리 공유하는 저장소를 만들고 싶다.", "특수교육과 4학년, 메이커 스페이스 활동 1년"),

    # ── 캠퍼스·대학생 (14) ──
    ("공강 공간 찾기", "공강 시간에 학교에서 쉴 곳을 찾다가 빈 강의실을 헤매는 일이 많다. 시간표상 비어 있는 강의실과 라운지 혼잡도를 알려 주는 서비스를 만들려 한다.", "컴퓨터공학과 3학년, 웹 개발 동아리 2년"),
    ("셔틀 도착 예측", "학교 셔틀버스가 교통 상황에 따라 들쭉날쭉해서 정류장에서 20분씩 기다리는 날이 있다. 실제 운행 기록으로 도착 시간을 예측해 알려 주는 앱.", "정보통신공학과 3학년, 파이썬 데이터 분석 학습"),
    ("주차면 안내", "통학하는 학생과 교직원이 몰리는 시간에 주차 자리를 못 찾아 갓길에 세우는 일이 반복된다. 층별 빈 주차면을 센서로 세어 진입 전에 알려 주는 시스템.", "전자공학과 4학년, 임베디드 프로젝트 경험"),
    ("공모전 팀 찾기", "공모전에 나가고 싶어도 필요한 역량을 가진 팀원을 주변에서 찾기 어려워 포기하는 경우가 많다. 관심 분야와 가능한 역할로 팀원을 연결해 주는 캠퍼스 서비스.", "경영학과 3학년, 교내 창업동아리 회장"),
    ("과제 일정 정리", "수업마다 공지 방식이 달라 과제와 시험 일정을 놓치는 일이 잦다. 강의계획서와 공지를 읽어 한 곳에 일정으로 모아 주는 도구를 만들고 싶다.", "소프트웨어학과 2학년, 안드로이드 앱 2개 제작"),
    ("전공 서적 교환", "한 학기 쓴 전공 책을 되팔 곳이 마땅치 않아 방에 쌓인다. 같은 학교·같은 과목 기준으로 책을 주고받는 캠퍼스 장터.", "문헌정보학과 3학년, 도서관 근로 2년"),
    ("스터디 매칭", "자격증 준비를 같이 할 사람을 찾으려면 단톡방을 뒤져야 하고 중간에 흐지부지되는 경우가 많다. 목표와 시간대를 맞춰 스터디를 만들고 출석을 기록하는 서비스.", "행정학과 3학년, 공무원 스터디 운영 1년"),
    ("교내 분실물", "강의실이나 도서관에서 물건을 두고 오면 어디에 문의해야 하는지 몰라 그냥 포기한다. 사진 한 장으로 등록하고 찾아가는 교내 분실물 서비스.", "산업공학과 2학년, 학생회 총무"),
    ("취업 정보 모으기", "졸업이 다가오는데 학과·취업지원팀·외부 사이트에 흩어진 공고를 매번 따로 확인해야 한다. 전공과 관심 직무로 걸러 한 곳에 모아 주는 서비스.", "경영정보학과 4학년, 인턴 6개월"),
    ("실험실 장비 예약", "공용 장비를 쓰려면 대학원생에게 물어봐야 하고 겹치면 하루를 버린다. 시간 단위로 예약하고 사용 기록이 남는 예약판을 만들려 한다.", "화학공학과 4학년, 연구실 장비 관리 1년"),
    ("기숙사 수리 신청", "기숙사에서 뭔가 고장 나면 어디에 어떻게 말해야 하는지 몰라 몇 주씩 그냥 쓴다. 사진을 찍으면 신청서가 만들어지고 처리 단계를 볼 수 있는 서비스.", "건축학과 3학년, 기숙사 자치회 1년"),
    ("동아리 회계", "동아리 회비를 엑셀로 관리하다 보니 인수인계 때마다 기록이 끊긴다. 영수증 사진으로 지출을 쌓고 학기말 결산을 자동으로 만들어 주는 도구.", "회계학과 3학년, 동아리 회계 2년"),
    ("강의 녹음 정리", "수업을 녹음해도 다시 듣기 부담스러워 결국 안 듣는다. 녹음에서 핵심만 뽑아 목차와 함께 정리해 주는 학습 도구.", "컴퓨터공학과 4학년, 음성 처리 프로젝트 경험"),
    ("자취 공동구매", "자취생은 쌀이나 세제처럼 대용량이 싼 물건을 혼자 사기 부담스럽다. 같은 건물·같은 동네 사람끼리 나눠 사는 공동구매 서비스.", "식품영양학과 3학년, 자취 4년"),

    # ── 생활·소비 서비스 (16) ──
    ("빵 재고 알림", "저녁에 빵집에 가면 먹고 싶던 빵이 늘 없다. 동네 빵집이 남은 품목을 두 번만 눌러 올리고 손님이 확인해 찾아가는 서비스를 만들고 싶다.", "식품영양학과 3학년, 베이커리 아르바이트 2년"),
    ("배달 온도 보장", "배달 음식이 식어서 오는 일이 잦은데 항의해도 확인할 방법이 없다. 포장 용기에 온도 표시를 붙여 도착 시점 온도를 남기는 배달 서비스.", "산업디자인학과 3학년, 포장 디자인 수업 이수"),
    ("분리배출 도우미", "품목마다 분리배출 방법이 다르고 지역마다 기준도 달라 매번 검색한다. 사진을 찍으면 품목을 알아보고 그 지역 기준을 알려 주는 앱.", "환경공학과 3학년, 이미지 분류 프로젝트 2건"),
    ("우산 대여 반납", "갑자기 비가 오면 편의점에서 우산을 또 사고 집에 우산이 쌓인다. 건물 출입구에서 빌리고 다른 제휴 장소에 반납하는 우산 공유 서비스.", "도시공학과 3학년, 지역 상권 조사 경험"),
    ("구독 정리", "여러 콘텐츠 구독료가 자동으로 빠져나가 얼마를 쓰는지 모른다. 결제 내역을 읽어 구독을 모아 보여 주고 안 보는 것을 알려 주는 서비스.", "경영학과 4학년, 핀테크 인턴 3개월"),
    ("영양제 추천", "운동을 시작하고 영양제를 찾아봤는데 정보가 광고뿐이라 뭘 먹어야 할지 모르겠다. 체형과 운동 목표를 넣으면 성분 기준으로 조합을 제안해 주는 서비스.", "생명과학과 3학년, 헬스 3년"),
    ("성분표 확인", "알레르기가 있는 가족이 있어 마트에서 성분표를 매번 확인하는데 글씨가 작고 오래 걸린다. 사진을 찍으면 미리 등록한 성분이 있는지 바로 알려 주는 앱.", "식품공학과 3학년, 식품기사 준비"),
    ("보험 청구 도우미", "병원에 다녀와 실손 청구를 하려면 서류를 잘 찍는 것부터 어렵다. 필요한 서류를 안내하고 사진을 정리해 제출까지 이어 주는 서비스.", "보건행정학과 3학년, 병원 원무과 근무 1년"),
    ("가구 배치 미리보기", "이사 전에 가구가 들어갈지 몰라 줄자로 재고 상상만 한다. 방 구조를 넣으면 가구 배치를 보여 주고 대안을 추천해 주는 서비스.", "실내건축학과 3학년, 3D 모델링 수업 이수"),
    ("코디 추천", "옷은 있는데 매일 뭘 입을지 정하는 데 시간이 오래 걸린다. 가진 옷을 등록하면 날씨와 일정에 맞춰 조합을 제안해 주는 앱.", "의류학과 3학년, 패션 편집숍 아르바이트 1년"),
    ("물건 짧게 빌리기", "캠핑 의자나 공구처럼 1년에 두어 번 쓰는 물건을 사기는 아깝고 빌릴 곳은 없다. 동네에서 하루 단위로 빌려주고 빌리는 서비스.", "경제학과 3학년, 중고 거래 커뮤니티 운영"),
    ("감정 기록 정리", "하루 기분을 기록하는 앱은 많은데 그 기록이 쌓여도 뭘 해야 할지 알려주지는 않는다. 기록에서 반복되는 상황을 짚어 주고 대처를 제안하는 서비스.", "심리학과 3학년, 상담 봉사 1년"),
    ("여행 정산", "여러 명이 여행을 가면 누가 뭘 냈는지 엉키고 정산이 늦어져 관계가 상한다. 결제 때마다 찍어 두면 마지막에 한 번에 정산해 주는 도구.", "관광경영학과 3학년, 여행 동아리 총무 2년"),
    ("독거 어르신 안부", "혼자 사는 어르신이 며칠 소식이 없어도 알기 어렵다. 전기 사용 패턴처럼 이미 있는 신호로 이상을 알아채 가족에게 알리는 서비스.", "사회복지학과 4학년, 노인복지관 실습"),
    ("전기차 충전 계획", "전기차로 장거리를 가면 어디서 충전할지 계산이 복잡하고 가서 보면 고장인 경우도 있다. 남은 배터리와 실제 가동 여부로 충전 지점을 짜 주는 서비스.", "자동차공학과 4학년, 전기차 동아리 2년"),
    ("혜택 알림", "청년 지원금이나 지역 혜택이 있어도 몰라서 못 받는 일이 많다. 나이·지역·상황을 넣으면 신청 가능한 것과 마감일을 알려 주는 서비스.", "행정학과 3학년, 주민센터 근로 1년"),

    # ── 지역·소상공인 (8) ──
    ("동네 반찬 꾸러미", "자취를 하면서 배달은 물리고 요리는 부담스러웠다. 동네 반찬가게가 그날 남은 반찬을 저녁에 할인 꾸러미로 내놓고 예약해 찾아가는 서비스.", "식품영양학과 4학년, 반찬가게 아르바이트 1년"),
    ("소상공인 게시글", "동네 식당 사장님은 사진은 찍어도 올릴 글을 쓸 시간이 없다. 메뉴 사진을 올리면 소개 글과 태그를 만들어 예약 발행해 주는 도구.", "미디어커뮤니케이션학과 3학년, SNS 운영 대행 경험"),
    ("농가 체험 예약", "주말 농장 체험은 전화로만 예약을 받아 놓치는 손님이 많다. 인근 농가를 묶어 시간대 예약과 수확물 배송을 함께 받는 서비스.", "원예학과 3학년, 농가 봉사 2년"),
    ("공유 주방 예약", "배달 창업을 하려면 주방 임대료가 부담이다. 심야에 비는 식당 주방을 시간 단위로 빌려주는 예약 서비스.", "외식경영학과 4학년, 주방 근무 2년"),
    ("전통시장 배달", "전통시장은 물건은 좋은데 주차와 들고 오는 게 힘들어 젊은 손님이 적다. 여러 점포 물건을 한 번에 담아 그날 배달해 주는 서비스.", "유통학과 3학년, 시장 상인회 조사 참여"),
    ("공실 팝업 매칭", "상가 공실은 비어 있고 창업 준비자는 시험 삼아 팔아 볼 곳이 없다. 짧게 빌려 팝업으로 써 보게 연결하는 서비스.", "부동산학과 3학년, 상권 분석 프로젝트"),
    ("무인점포 관리", "무인점포가 늘었는데 재고와 사고 확인을 사장이 밤에 직접 본다. 카메라 기록에서 확인이 필요한 장면만 뽑아 알려 주는 서비스.", "정보보호학과 3학년, 영상 처리 학습"),
    ("지역 축제 정보", "지역 축제는 포스터와 블로그에 흩어져 있어 지나고 나서야 안다. 반경 기준으로 이번 주 행사를 모아 알려 주는 서비스.", "관광학과 2학년, 지역 홍보 서포터즈"),

    # ── 건강·안전·기타 (4) ──
    ("자세 알림", "하루 종일 앉아 있다 보면 자세가 무너지는데 스스로는 모른다. 의자에 붙이는 센서로 자세를 재고 오래 굳어 있으면 알려 주는 장치.", "물리치료학과 3학년, 재활 실습 6개월"),
    ("해양 안전 신호", "낚시터나 갯벌에서 물때를 놓쳐 고립되는 사고가 매년 난다. 위치와 물때를 계산해 나올 시간을 미리 알려 주는 장치와 앱.", "해양경찰학과 3학년, 인명구조 자격"),
    ("공사장 소음 기록", "공사 소음으로 민원을 넣어도 그때그때 측정이 안 돼 근거가 없다. 주민이 직접 소음을 기록해 시간대별로 정리해 주는 서비스.", "환경보건학과 3학년, 측정 실습 경험"),
    ("사내 매뉴얼 검색", "신입이 들어오면 매뉴얼이 흩어져 있어 매번 선배에게 묻는다. 사내 문서에서 질문에 맞는 부분만 찾아 주는 도구.", "산업경영공학과 4학년, 중소기업 인턴 6개월"),
]

# ── 외국인 20 (name, idea, capability) — 실제 외국인 학생 입력과 같은 결 ──
FR_IDEAS: list[tuple[str, str, str]] = [
    ("gym-qr", "I scan a QR code on a treadmill or bike at the gym and get matched with other people working out nearby at the same time, then we run a real-time distance race. Many students quit after two weeks because working out alone is boring.", "Two years of part-time work at a fitness center, basic Flutter development, ran a running crew of 40 people."),
    ("lab-share", "Foreign graduate students cannot find which lab equipment is free at what time, so they waste days waiting. I want a booking board for shared instruments that shows availability by hour and sends a reminder before the slot.", "Master's student in chemical engineering, managed the equipment log for my lab for one year."),
    ("halal-map", "Muslim students in Cheonan struggle to find halal food and existing maps are outdated or Korean-only. I want an app where students verify restaurants together and menus are translated into English, Arabic and Indonesian.", "Ran a 900-member international student group, restaurant part-time work for 18 months."),
    ("visa-jobs", "Part-time job postings are Korean-only and employers worry about visa rules, so foreign students end up in unsafe work. I want to list only jobs legal for D-2 and D-4 holders with a weekly working-hours checker.", "TOPIK level 4, two years of restaurant work in Korea, admin of a 1,500-member student group."),
    ("recycle-cam", "People want to recycle properly but do not know which bin an item goes into, and rules change by city. I want a camera app that names the item, shows the local rule, and gives a weekly score for each dormitory floor.", "Computer science undergraduate, built two small image classifiers for class."),
    ("study-buddy", "Language exchange at our university is arranged on paper forms and most pairs stop after one meeting. I want an app that matches by level and schedule, suggests a topic for each meeting, and tracks sessions.", "Exchange student, tutored Korean-English conversation for three semesters."),
    ("food-waste", "Restaurants waste food and time because they cannot predict how many customers will come. I want a simple tool that reads past sales and weather and suggests how much to prepare each morning.", "Worked at a bakery cafe and a cosmetics shop, studying business administration."),
    ("dorm-fix", "When something breaks in the dormitory, foreign students do not know how to report it in Korean and wait for weeks. I want a photo-based repair request that writes the Korean report and shows progress.", "Lived in three dormitories, volunteered as a floor representative for a year."),
    ("clinic-guide", "Going to a hospital in Korea is stressful because I cannot explain symptoms and do not know which department to visit. I want an app that turns my symptoms into a Korean sentence and suggests the right department nearby.", "Nursing student, interpreted for classmates at a health center for a year."),
    ("bank-steps", "Opening a bank account or changing a phone plan needs documents that nobody explains in my language. I want a checklist app that shows each step, the papers needed, and what to say at the counter.", "Business administration student, helped 30 new students settle in."),
    ("used-market", "Graduating international students throw away furniture and appliances because selling them is hard without Korean. I want a campus second-hand market with automatic translation and pickup arranged between students.", "Ran a dormitory trading chat group of 400 people."),
    ("bus-english", "Intercity bus and train booking sites are hard to use in English and refund rules are unclear. I want a guide that shows the booking steps and the refund rule for the exact ticket I bought.", "Tourism major, traveled to 15 Korean cities and wrote a blog about it."),
    ("cook-share", "Dormitory kitchens are empty at night and many students eat instant food because cooking alone is expensive. I want a service where students cook together in shifts and share ingredient costs.", "Culinary arts student, worked in a hotel kitchen for one year."),
    ("class-notes", "Lectures are fast and technical terms are hard, so international students fall behind even when they understand the subject. I want shared notes where the key terms are explained in simple Korean and English.", "Electrical engineering student, kept a shared study document for 60 classmates."),
    ("part-time-rate", "It is hard to know whether a part-time job pays the legal minimum and how overtime should be counted. I want a calculator that checks my contract and hours against the law and warns me.", "Law major, volunteered at a migrant worker support center for a year."),
    ("prayer-space", "Finding a quiet place to pray between classes is difficult and asking each building office is awkward. I want a map of quiet rooms that students confirm and update themselves.", "Architecture student, surveyed campus buildings for a class project."),
    ("home-food", "Ingredients from my home country are sold in a few shops that are far away and often out of stock. I want a group order service where students in the same city order together every two weeks.", "Ran a group purchase chat for two years, food science major."),
    ("mentor-match", "New international students repeat the same mistakes because there is no one to ask in their first month. I want to match them with a senior from the same country and department for eight weeks.", "Third-year student, mentored 12 juniors informally."),
    ("photo-doc", "Immigration and school documents must be filled in Korean and a small mistake means going back again. I want an app that reads the form by camera and fills it with my saved information.", "Computer engineering student, built a small OCR project."),
    ("weather-wear", "Korean weather changes fast and I keep dressing wrong, which made me sick twice. I want a simple daily suggestion based on the forecast and what I own, written in my language.", "Fashion design student, kept a clothing inventory app for myself."),
]


# 템플릿마다 숫자를 **두 개씩** 쓴다 — 조합 수가 회차 간 충돌을 좌우한다(아래 unique_tail 주석).
_KR_TAILS = [
    " 지금까지 주변 {a}명에게 물어봤고 그중 {c}명은 바로 쓰겠다고 했다.",
    " 우선 {b}주 안에 시제품을 만들어 {a}명에게 보여 줄 생각이다.",
    " 첫 목표는 이용자 {a}명이고 준비 기간은 {b}주로 보고 있다.",
    " 관련해서 {c}곳을 직접 찾아가 이야기를 들어봤고 {a}명분의 의견을 모았다.",
    " 초기 예산은 {a}만 원 안에서 잡고 {b}주 동안 시험해 보려 한다.",
    " 비슷한 서비스를 {c}개 써 봤는데 {a}명에게 물어보니 같은 부분이 빠져 있다고 했다.",
]
_FR_TAILS = [
    " I asked {a} people around me and {c} of them said they would use it right away.",
    " I want to build a prototype within {b} weeks and show it to {a} students.",
    " My first target is {a} users and I plan to spend {b} weeks preparing.",
    " I visited {c} places and collected opinions from {a} people.",
    " I want to start with a budget under {a} man won and test it for {b} weeks.",
    " I tried {c} similar services and {a} people told me the same part was missing.",
]


def unique_tail(rng: random.Random, foreign: bool = False) -> str:
    """아이디어 끝에 붙이는 한 문장 — **조사 캐시(7일)를 확실히 비껴가게 하는 것이 목적**이다.

    캐시 키가 `sha1(track|idea|question_id)` 라 아이디어가 한 글자만 달라도 새로 조사한다.
    다만 문장이 어색하면 생성 품질 측정이 흔들리므로, 학생이 실제로 쓸 법한 문장에 숫자만 바꾼다.
    조합 수 ≈ 6 × 296 × 39 ≈ 7만 (템플릿마다 숫자 두 개). 3회차 × 60명 = 180번 뽑아도 겹칠 확률이 0.1% 아래다 —
    숫자를 하나만 쓰던 초안은 3천 조합뿐이라 회차 간 충돌이 실제로 났다(설계 중 계산으로 발견).
    캐시를 **일부러 태우고 싶으면**(같은 아이디어를 여러 명이 낸 상황 재현) 호출하지 않으면 된다.
    """
    tails = _FR_TAILS if foreign else _KR_TAILS
    return rng.choice(tails).format(a=rng.randint(5, 300), b=rng.randint(2, 30), c=rng.randint(2, 40))
