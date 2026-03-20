import os
import time
import re
import uuid
import threading
import urllib.parse
import pandas as pd
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, render_template, jsonify
from cachetools import TTLCache
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

app = Flask(__name__)
# 실행 위치와 무관하게 현재 스크립트(jemini_food.py)가 있는 디렉토리를 기준으로 templates 폴더를 찾도록 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'), static_folder=os.path.join(BASE_DIR, 'static'))

# --- 💡 로깅 설정 테스트 ---
logger = logging.getLogger('CrawlerUsage')
logger.setLevel(logging.INFO)
log_path = 'usage.log' 
try:
    file_handler = RotatingFileHandler(log_path, maxBytes=1024*1024, backupCount=5)
    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    print(f"로깅 설정 오류: {e}")

# --- 💡 캐싱 및 작업 분리 ---
search_cache = TTLCache(maxsize=100, ttl=3600)
scrape_progress = {}

# --- 💡 동시성 및 리소스 관리 설정 ---
# 1. 매 요청마다 호출되던 드라이버 설치를 전역(앱 실행 시 1회)으로 변경하여 속도 향상
DRIVER_PATH = ChromeDriverManager().install()
# 2. 2GB 서버 RAM 초과(OOM) 방지를 위해 크롬 브라우저 개수를 2개로 엄격히 제한
MAX_CONCURRENT_BROWSERS = 2
browser_semaphore = threading.Semaphore(MAX_CONCURRENT_BROWSERS)

def crawl_kakao_map(region_query, max_pages, job_id):
    cache_key = f"{region_query}_{max_pages}"
    if cache_key in search_cache:
        scrape_progress[job_id] = {"status": "cached", "current": max_pages, "total": max_pages}
        return search_cache[cache_key]

    scrape_progress[job_id] = {"current": 0, "total": max_pages, "status": "waiting_for_browser"}
    restaurant_list = []
    
    # 💡 세마포어를 통해 동시 브라우저 실행 수를 제한 (대기열 관리)
    with browser_semaphore:
        scrape_progress[job_id]["status"] = "initializing"
        
        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')                        # 최신 헤드리스 모드 (메모리 안정성 향상)
        options.add_argument('--window-size=1920,1080')               # 모바일 UI로 변경되어 리스트가 숨겨지는 현상 방지
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')               # /dev/shm 대신 /tmp 사용 (메모리 부족 방지)
        options.add_argument('--disable-gpu')                 # 불필요한 GPU 연산 비활성화
        options.add_argument('--disable-extensions')          # 확장 프로그램 비활성화
        options.add_argument('--blink-settings=imagesEnabled=false')  # 이미지 로딩 완전 차단 (속도 향상 및 메모리 절약)
        options.add_argument('--js-flags="--max-old-space-size=256"') # V8 자바스크립트 엔진 힙 메모리 256MB로 엄격히 제한
        options.add_argument('--disable-software-rasterizer')
        
        # 💡 브라우저 구동 시 프록시 자동 탐색으로 인한 20초 지연(Hang) 현상 완벽 방지
        options.add_argument('--proxy-server="direct://"')
        options.add_argument('--proxy-bypass-list=*')
        options.add_argument('--disable-dbus')                # 💡 리눅스 DBus 타임아웃(초기 구동 20초 지연) 완벽 방지
        options.add_argument('--disable-background-networking')
        options.add_argument('--no-first-run')
        options.page_load_strategy = 'eager'
        
        # 💡 극한의 속도 최적화: 텍스트 크롤링에 불필요한 모든 리소스 차단
        prefs = {
            'profile.managed_default_content_settings': {
                'images': 2,        # 이미지 차단
                'plugins': 2,       # 플러그인 차단
                'media_stream': 2,  # 미디어 차단
                'stylesheet': 2,    # CSS(스타일시트) 로딩 차단 -> 렌더링 속도 대폭 향상
                'font': 2,          # 웹 폰트 다운로드 차단
                'sub_frame': 2      # IFrame(외부 위젯/광고 등) 차단
            }
        }
        options.add_experimental_option('prefs', prefs)
        options.add_argument('--disable-logging')
        options.add_argument('--log-level=3')

        # 전역 캐싱된 드라이버 경로 사용
        driver = webdriver.Chrome(service=Service(DRIVER_PATH), options=options)
        
        try:
            driver.implicitly_wait(3)
            wait = WebDriverWait(driver, 5) # 최대 5초 대기
            driver.get("https://map.kakao.com/")
            
            search_box = driver.find_element(By.ID, "search.keyword.query")
            search_box.send_keys(region_query)
            search_box.send_keys(Keys.ENTER)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.PlaceItem"))) # 검색 결과가 뜰 때까지 대기
            
            # 💡 스마트 대기: 무조건 2초를 기다리지 않고, 별점 데이터가 로딩되면 즉시 통과 (최대 2초)
            for _ in range(10):
                if any(e.text.strip() not in ['', '0.0'] for e in driver.find_elements(By.CSS_SELECTOR, "em.num")):
                    time.sleep(0.5) # 💡 첫 식당 별점 렌더링 직후, 나머지 14개 식당의 데이터도 DOM에 채워질 수 있도록 0.5초 여유 부여
                    break
                time.sleep(0.2)
            
            scrape_progress[job_id]["status"] = "scraping"
            
            current_page = 1
            while current_page <= max_pages:
                scrape_progress[job_id]["current"] = current_page
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.PlaceItem")))
                
                # 💡 스마트 대기: 페이지 이동 후 별점 데이터 로딩 시 즉시 통과
                for _ in range(10):
                    if any(e.text.strip() not in ['', '0.0'] for e in driver.find_elements(By.CSS_SELECTOR, "em.num")):
                        time.sleep(0.5) # 💡 나머지 식당 DOM 렌더링 대기
                        break
                    time.sleep(0.2)
                
                soup = BeautifulSoup(driver.page_source, 'html.parser')
                places = soup.select("li.PlaceItem")
                
                for place in places:
                    try:
                        name_tag = place.select_one("a.link_name")
                        if not name_tag:
                            continue
                        name = name_tag.text.strip()
                        
                        category_tag = place.select_one("span.subcategory")
                        category = category_tag.text.strip() if category_tag else ""
                        
                        link_tag = place.select_one("a.moreview")
                        link = link_tag.get('href', '#') if link_tag else '#'
                        
                        rating = 0.0
                        rating_tag = place.select_one("em.num")
                        if rating_tag and rating_tag.text and rating_tag.text != '0.0':
                            try: rating = float(rating_tag.text)
                            except: pass
                        
                        rating_count = 0
                        # 💡 블로그/방문자 '리뷰'는 제외하고 오직 '별점/후기 참여 수'만 집계
                        rating_count_tag = place.select_one("a[data-id='numberofscore']") or place.select_one(".rating .numberofscore")
                        if rating_count_tag:
                            cnt_str = re.sub(r'[^0-9]', '', rating_count_tag.text)
                            if cnt_str:
                                try: rating_count = int(cnt_str)
                                except: pass
                        
                        addr_tag = place.select_one("div.info_item > div.addr > p")
                        address = addr_tag.text.strip() if addr_tag else ""
                        
                        restaurant_list.append({"페이지": current_page, "상호명": name, "업종": category, "평점": rating, "후기수": rating_count, "주소": address, "링크": link})
                    except:
                        # 데이터 파싱 중 일부 오류가 나도 해당 식당을 통째로 날리지 않고 계속 진행
                        pass
                        
                if current_page == max_pages:
                    break
                
                # --- 다음 페이지 이동 로직 ---
                try:
                    first_item = driver.find_element(By.CSS_SELECTOR, "li.PlaceItem")
                except:
                    break # 리스트가 아예 없으면 더 이상 진행 불가
                
                clicked_more = False
                try:
                    # 1. '장소 더보기' 버튼이 있다면 먼저 클릭 (최초 리스트업 수집 후 확장)
                    more_button = driver.find_element(By.ID, "info.search.place.more")
                    if more_button.is_displayed() and "HIDDEN" not in more_button.get_attribute("class").upper():
                        driver.execute_script("arguments[0].click();", more_button)
                        clicked_more = True
                        wait.until(EC.staleness_of(first_item))
                except:
                    pass
                
                if clicked_more:
                    current_page += 1
                    continue
                    
                try:
                    # 2. '장소 더보기'가 없다면 현재 UI 상 활성화된 페이지를 기준으로 다음 페이지 클릭
                    active_page_element = driver.find_element(By.XPATH, "//*[@id='info.search.page']//*[contains(@class, 'ACTIVE')]")
                    ui_current_page = int(active_page_element.text)
                    ui_next_page = ui_current_page + 1
                    
                    if ui_next_page % 5 == 1:
                        # 5, 10, 15... 페이지 이후 '다음' 그룹 버튼 클릭
                        next_btn = driver.find_element(By.ID, "info.search.page.next")
                        if "DISABLED" in next_btn.get_attribute("class").upper():
                            break
                        driver.execute_script("arguments[0].click();", next_btn)
                    else:
                        # 일반 페이지 번호 클릭
                        page_btn_id = f"info.search.page.no{ui_next_page % 5 if ui_next_page % 5 != 0 else 5}"
                        page_btn = driver.find_element(By.ID, page_btn_id)
                        if "HIDDEN" in page_btn.get_attribute("class").upper() or "DISABLED" in page_btn.get_attribute("class").upper():
                            break
                        driver.execute_script("arguments[0].click();", page_btn)
                        
                    wait.until(EC.staleness_of(first_item))
                    current_page += 1
                except:
                    break # 페이지네이션 요소가 더 없으면 완전히 종료
        finally:
            driver.quit()

    search_cache[cache_key] = restaurant_list
    return restaurant_list
# --- Flask 웹 라우팅 ---
@app.route('/progress/<job_id>')
def progress(job_id): 
    return jsonify(scrape_progress.get(job_id, {"status": "ready", "current": 0, "total": 0}))

@app.route('/', methods=['GET', 'POST'])
def index():
    table_html, query, max_pages, exclude_words, elapsed_time = "", "", 15, "", "0"
    current_job_id = str(uuid.uuid4())
    
    if request.method == 'POST':
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        query = request.form.get('query', '')
        max_pages = int(request.form.get('max_pages', 15))
        exclude_words = request.form.get('exclude_words', '')
        current_job_id = request.form.get('job_id', current_job_id)
        
        if query.strip():
            start_t = time.time()
            try:
                data = crawl_kakao_map(query, max_pages, current_job_id)
                df = pd.DataFrame(data)
                
                num_res = 0
                if not df.empty and '상호명' in df.columns:
                    df = df.drop_duplicates(subset=['상호명', '주소']) # 이름이 같아도 지점(주소)이 다르면 누락되지 않도록 수정
                    
                    if exclude_words.strip():
                        pattern = '|'.join([re.escape(w.strip()) for w in exclude_words.split(',') if w.strip()])
                        df = df[~df['업종'].str.contains(pattern, na=False)]
                    
                    final_df = df[df['평점'] >= 4.0].sort_values(by='후기수', ascending=False).head(10)
                    num_res = len(final_df)
                    
                    if not final_df.empty:
                        final_df.insert(0, '순위', range(1, num_res + 1))
                        
                        final_df['상호명'] = '<a href="' + final_df['링크'] + '" target="_blank">' + final_df['상호명'] + '</a>'
                        final_df['업종'] = '<span class="clickable-category" onclick="addExcludeWord(\'' + final_df['업종'] + '\')" title="클릭하여 제외 업종에 추가">' + final_df['업종'] + '</span>'
                        
                        # 💡 주소 텍스트를 카카오맵 공식 길찾기(도착지 설정) 링크로 완벽 변환
                        def make_route_link(row):
                            addr = row['주소']
                            link = str(row['링크'])
                            
                            # 장소 고유 ID(숫자)를 추출하여 카카오 공식 길찾기 URL 생성
                            if "place.map.kakao.com/" in link:
                                place_id = link.split("/")[-1]
                                url = f"https://map.kakao.com/link/to/{place_id}"
                            else:
                                # ID 추출 실패 시 예외 처리 (검색 방식 유지)
                                url = f"https://map.kakao.com/?eName={urllib.parse.quote(addr)}"
                                
                            return f'<a href="{url}" target="_blank" title="길찾기로 이동" style="color:#2c3e50; font-weight:500; text-decoration:underline;">{addr}</a>'

                        # axis=1을 주어 행 전체(row) 데이터를 함수로 넘깁니다.
                        final_df['주소'] = final_df.apply(make_route_link, axis=1)
                        
                        table_html = final_df[['순위', '상호명', '업종', '평점', '후기수', '주소']].to_html(escape=False, index=False, border=0)
                
                elapsed_time = f"{time.time() - start_t:.2f}"
                
                try: logger.info(f"IP:{user_ip}|Q:'{query}'|P:{max_pages}|R:{num_res}|T:{elapsed_time}s")
                except: pass
            finally:
                if current_job_id in scrape_progress:
                    del scrape_progress[current_job_id]

    new_job_id = str(uuid.uuid4())
    return render_template('index.html', table_html=table_html, query=query, max_pages=max_pages, exclude_words=exclude_words, elapsed_time=elapsed_time, job_id=new_job_id)

if __name__ == '__main__':
    try:
        from waitress import serve
        logger.info("Waitress(프로덕션 WSGI) 서버로 실행 중입니다. (포트: 5000)")
        serve(app, host='0.0.0.0', port=5000, threads=4)
    except ImportError:
        logger.warning("waitress 모듈이 없습니다. 프로덕션 환경을 위해 'pip install waitress'를 권장합니다.")
        app.run(host='0.0.0.0', port=5000, threaded=True)