"""여정 버프 효과 설명 보충 데이터 (옛 DB star-savior-arcana-db 백업 2026-10-15 의 journey_buffs.json 에서 추출).
새 DB(arcanas.json journeyBuffs)에 없는 이름만 InfoIndex 가 보충해서 쓴다."""

BUFF_DESC = {'ko': {'분노': '화를 주체할 수 없습니다. 훈련 실패율이 증가합니다.',
        '게으름': '내일의 나에게 맡깁니다. 훈련 실패율이 증가합니다.',
        '절망감': '이제 꿈도 희망도 없습니다. 훈련 실패율이 증가합니다.',
        '혼란': '지금까지 믿어오던 가치관이 흔들리고 있습니다. 훈련 실패율이 증가합니다.',
        '고독감': '막아 보려고 해도, 마음 속이 차갑게 식는 것을 느낍니다. 훈련 실패율이 증가합니다.',
        '흥청망청': '뒤를 생각하지 않고 현재를 즐기고 있습니다. 훈련 실패율이 증가합니다.',
        '자신감': '아무도 막을 수 없습니다. 훈련 실패율이 감소합니다.',
        '행복': '모든 것이 아름답습니다. 훈련 실패율이 감소합니다.',
        '쑥스러움': '떠올리면 웃음이납니다. 훈련 실패율이 감소합니다.',
        '고마움': '따뜻한 마음에 힘이 솟아납니다. 훈련 실패율이 감소합니다.',
        '즐거움': '모든 일이 잘 풀릴 것 같습니다. 훈련 실패율이 감소합니다.',
        '깨달음': '생각하지 못한 것들이 떠오릅니다. 훈련 실패율이 감소합니다.',
        '뿌듯함': '떠올리면 흐뭇해집니다. 훈련 실패율이 감소합니다.',
        '일일 아르바이트생': '아포칼립스 상단의 일일 구원자. 거래 시 상품 구매 가격이 감소합니다.',
        '우수 고객': '가게에 방문하는 것 만으로 점원이 마중 나올 정도입니다. 거래 시 상품 구매 가격이 감소합니다.',
        '훈련 능숙 : 각성': '완벽한 휴식으로 모든 훈련이 능숙해집니다. 모든 훈련의 실패율이 감소합니다.',
        '훈련 능숙 : 힘': '충분한 휴식으로 훈련이 능숙해집니다. 훈련 중 획득하는 힘이 증가합니다.',
        '훈련 능숙 : 체력': '충분한 휴식으로 훈련이 능숙해집니다. 훈련 중 획득하는 체력이 증가합니다.',
        '훈련 능숙 : 인내': '충분한 휴식으로 훈련이 능숙해집니다. 훈련 중 획득하는 인내가 증가합니다.',
        '훈련 능숙 : 집중': '충분한 휴식으로 훈련이 능숙해집니다. 훈련 중 획득하는 집중이 증가합니다.',
        '훈련 능숙 : 보호': '충분한 휴식으로 훈련이 능숙해집니다. 훈련 중 획득하는 보호가 증가합니다.',
        '전설의 구원자': '오늘만큼은 전설의 구원자가 된 기분입니다. 훈련 중 획득하는 잠재력 포인트가 증가합니다.',
        '흑역사 : 플로라': '누구나 기억하고 싶지않은 과거는 있는 법입니다. 훈련 중 획득하는 힘이 감소합니다.',
        '흑역사 : 칼라이드': '누구나 기억하고 싶지않은 과거는 있는 법입니다. 훈련 중 획득하는 체력이 감소합니다.',
        '상쾌함': '부담감을 떨쳐내고 새롭게 시작합니다. 훈련 실패율이 감소합니다.',
        '마력 탈진': '기운이 없어서 서 있기도 어렵습니다. 훈련 실패율이 크게 증가합니다.',
        '고르디우스의 검': '어떤 난관도 두렵지 않습니다. 훈련 실패율이 감소합니다.',
        '급성 슬럼프': '돌이켜 보면 허망한 삶이었습니다. 훈련 실패율이 증가합니다.',
        '데드라인 효과': '압박감이 긍정적인 영향을 줍니다. 훈련 중 획득하는 잠재력 포인트가 증가합니다.',
        '과욕': '무리한 훈련이 독이 되었습니다.  훈련 실패율이 증가합니다.',
        '제약을 이겨내고': '몸에 걸리는 부하도 일상 같습니다. 훈련 중 획득하는 힘이 증가합니다.',
        '무모한 도전': '강적을 꺾을 때까지 도전은 계속됩니다. 훈련 중 획득하는 체력이 증가합니다.',
        '힘의 자양분': '후대에 계승될 힘을 제공하고 있습니다. 훈련 중 획득하는 체력이 감소합니다.',
        '체력의 자양분': '후대에 계승될 체력을 제공하고 있습니다. 훈련 중 획득하는 힘이 감소합니다.',
        '리세트와의 추억': '솜사탕처럼 달콤했던 기억이 생각납니다. 훈련 실패율이 감소합니다.',
        '트렌드 팔로워': '유행하는 것이라면 적어도 구매는 해봐야합니다. 훈련 실패율이 감소합니다.',
        '번 아웃': '열정이 과했던 나머지 불타버렸습니다. 훈련 중 체력을 획득 할 수 없습니다.',
        '근육 부상': '움직이지 않는게 제일 좋습니다. 힘 훈련 경험치 획득량이 감소합니다.',
        '과식': '너무 먹어서 몸이 둔해졌습니다. 훈련 중 획득하는 힘이 감소합니다.',
        '불굴의 정신': '중요한 것은 포기하지 않는 정신입니다. 훈련 중 획득하는 인내가 증가합니다.',
        '눈썰미': '관찰력 하나만큼은 확실히 좋습니다. 훈련 중 획득하는 집중이 증가합니다.',
        '영양식': '균형 잡힌 식사는 중요합니다. 체력 훈련 경험치 획득량이 증가합니다.',
        '탕진': '전재산을 모두 소진했더니, 도저히 의욕이 나질 않습니다. 훈련 중 획득하는 힘이 감소합니다.',
        '작전 방침': '능숙한 지휘로 현장을 조율합니다. 훈련 실패율이 감소합니다.',
        '책임감': '별의별 진상에도 불구하고 책임감을 잃지 않고, 끝까지 마감 시간을 준수했습니다. 훈련 실패율이 감소합니다.',
        '슈거 하이': '무의식 속 욕망을 마음껏 해소했습니다. 훈련 중 획득하는 잠재력 포인트가 증가합니다.',
        '생존왕': '어떤 터프한 상황도 이겨내는 근성이 차오릅니다. 힘 훈련 경험치 획득량이 증가합니다.',
        '메타 인지': '이 세계가 게임이라는 믿을 수 없는 이야기를 들었습니다. 혼란스러워 집중이 잘 되지 않습니다… 훈련 중 획득하는 집중이 감소합니다.',
        '달콤한 여운': '가끔씩 생각나는 달콤한 맛입니다. 훈련 중 획득하는 집중이 증가합니다.',
        '새콤한 여운': '가끔씩 생각나는 새콤한 맛입니다. 훈련 중 획득하는 보호가 증가합니다.',
        '풍부한 여운': '가끔씩 생각나는 풍부한 맛입니다. 훈련 중 획득하는 인내가 증가합니다.',
        '이상업ㅂ음!': '괜찮다고 하는 사람치고 정말 괜찮은 사람은 없습니다. 훈련 실패율이 크게 증가합니다.',
        '총명한 여정 단원': '훈련 실패율이 조금 줄어듭니다.',
        '공감 능력': '다른 사람의 마음을 깊이 이해할 줄 아는 여정 단원. 훈련 중 획득하는 인연 포인트가 증가합니다.',
        '활발함': '훈련으로 획득하는 힘이 증가합니다.',
        '지구력 증진': '훈련으로 획득하는 체력이 조금 증가합니다.',
        '포용력': '훈련으로 획득하는 인내가 조금 증가합니다.',
        '탈력': '충분한 휴식을 취하지 못해 힘이 나질 않습니다.\n훈련으로 획득하는 힘이 조금 감소합니다.',
        '피로감': '충분한 휴식을 취하지 못해 피로감이 느껴집니다.\n훈련으로 획득하는 체력이 조금 감소합니다.',
        '비몽사몽': '충분한 휴식을 취하지 못해 잠이 깨질 않습니다.\n훈련으로 획득하는 인내가 조금 감소합니다.'},
 'en': {'Rage': 'You cannot contain your anger. Training failure rate increases.',
        'Laziness': 'You leave it to your future self. Training failure rate increases.',
        'Despair': 'Dreams? Hopes? What are those? Training failure rate increases.',
        'Confusion': 'Your long-held values are being shaken. Training failure rate increases.',
        'Loneliness': 'No matter how you try to resist, you feel your heart growing cold. Training failure '
                      'rate increases.',
        'Living It Up': "You're enjoying the moment without thinking about what comes next. Training failure "
                        'rate increases.',
        'Confidence': 'You feel unstoppable. Training failure rate decreases.',
        'Happiness': 'Everything feels beautiful. Training failure rate decreases.',
        'Shyness': 'Memories of the past conjure smiles. Training failure rate decreases.',
        'Gratitude': 'Warm feelings of gratitude fill you with strength. Training failure rate decreases.',
        'Joy': 'You feel that everything will go well. Training failure rate decreases.',
        'Realization': 'Unexpected realizations strike you. Training failure rate decreases.',
        'Pride': 'Memories of the past fill you with great satisfaction. Training failure rate decreases.',
        'Daily Part-Timer': 'You are the daily Savior of Apocalypse Shop. Shop prices are reduced.',
        'VIP Customer': 'The staff come out to greet you just for visiting the shop. Shop prices are '
                        'reduced.',
        'Training Proficiency: Awakening': 'A perfect night of rest makes you ready to ace any training. '
                                           'Failure rate of all training decreases.',
        'Training Proficiency: Strength': 'Sufficient rest improves training effectiveness. Strength gained '
                                          'during training increases.',
        'Training Proficiency: Vitality': 'Sufficient rest improves training effectiveness. Vitality gained '
                                          'during training increases.',
        'Training Proficiency: Endurance': 'Sufficient rest improves training effectiveness. Endurance '
                                           'gained during training increases.',
        'Training Proficiency: Focus': 'Sufficient rest improves training effectiveness. Focus gained during '
                                       'training increases.',
        'Training Proficiency: Protection': 'Sufficient rest improves training effectiveness. Protection '
                                            'gained during training increases.',
        'Legendary Savior': 'You feel like a legendary Savior today. Potential Points gained during training '
                            'increase.',
        'Shameful Memories: Flora': 'Everyone has a past they would rather forget. Strength gained during '
                                    'training decreases.',
        'Shameful Memories: Kalaid': 'Everyone has a past they would rather forget. Vitality gained during '
                                     'training decreases.',
        'Freshness': 'You shake off your burdens to make a fresh start. Training failure rate decreases.',
        'Mana Exhaustion': 'You feel too weak to even stand. Training failure rate greatly increases.',
        'Sword of Gordius': 'You fear no challenge. Training failure rate decreases.',
        'Acute Slump': 'Looking back, life feels futile. Training failure rate increases.',
        'Deadline Effect': 'Pressure has a positive effect. Potential Points gained during training '
                           'increase.',
        'Overexertion': 'Overtraining has taken its toll. Training failure rate increases.',
        'Surpassing Limits': 'You feel accustomed to the strain on your body. Strength gained during '
                             'training increases.',
        'Reckless Challenge': 'You keep challenging yourself until powerful foes are defeated. Vitality '
                              'gained during training increases.',
        'Strength Nourishment': 'You provide strength to be passed down to future generations. Vitality '
                                'gained during training decreases.',
        'Vitality Nourishment': 'You provide vitality to be passed down to future generations. Strength '
                                'gained during training decreases.',
        'Memories with Lisette': 'Sweet, cotton candy-like memories come to mind. Training failure rate '
                                 'decreases.',
        'Trend Chaser': "If it's all the rage, you have to see what all the fuss is about. Training failure "
                        'rate decreases.',
        'Burnout': 'Your excessive passion has burned you out. You cannot gain Vitality during training.',
        'Muscle Injury': 'You feel that staying still is best. Strength Training EXP gained decreases.',
        'Overeating': 'You feel sluggish from overeating. Strength gained during training decreases.',
        'Indomitable Spirit': 'What matters most is a spirit that never gives up. Endurance gained during '
                              'training increases.',
        'Keen Eye': 'Your observational skills are excellent. Focus gained during training increases.',
        'Nutritious Meal': 'A balanced diet is important. Vitality Training EXP gained increases.',
        'All-In': 'After spending everything you had, you feel completely unmotivated. Strength gained from '
                  'training decreases.',
        'Operation Policy': 'Coordinates the field with skillful command. Training failure rate decreases.',
        'Responsibility': 'No matter how absurd things turn out, you consistently meet your deadlines until '
                          'the very end. Training failure rate decreases.',
        'Sugar Rush': 'Subconscious desires have been fully indulged. Potential Points gained during '
                      'training increase.',
        'King of Survival': 'Your grit to endure any hardship grows stronger. Strength training EXP gained '
                            'increases.',
        'Metacognition': "I've heard the unbelievable claim that this world is a game. It's confusing, and I "
                         "can't focus… Focus gained during training decreases.",
        'Sweet Aftertaste': 'You recall a sweet flavor. Focus gained during training increases.',
        'Tangy Aftertaste': 'You recall a tangy flavor. Protection gained during training increases.',
        'Rich Aftertaste': 'You recall a rich flavor. Endurance gained during training increases.',
        "I'm F-F-Fine!": 'Those who say they are fine are rarely truly fine. Training failure rate greatly '
                         'increases.',
        'Brilliant Journey Member': 'Training failure rate slightly decreases.',
        'Empathy': 'You deeply understand the hearts of others. Bond Points gained during training increase.',
        'Liveliness': 'Strength gained from training increases.',
        'Endurance Boost': 'Vitality gained from training slightly increases.',
        'Tolerance': 'Endurance gained from training slightly increases.',
        'Exhaustion': 'You feel drained from insufficient rest.\n'
                      'Strength gained from training slightly decreases.',
        'Fatigue': 'You feel fatigued from insufficient rest.\n'
                   'Vitality gained from training slightly decreases.',
        'Drowsiness': 'You feel drowsy from insufficient rest.\n'
                      'Endurance gained from training slightly decreases.'}}
