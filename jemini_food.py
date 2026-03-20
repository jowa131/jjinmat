import time
import re
import uuid
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

def crawl_kakao_map(region_query, max_pages, job_id):
    cache_key = f"{region_query}_{max_pages}"
    if cache_key in search_cache:
        scrape_progress[job_id] = {"status": "cached", "current": max_pages, "total": max_pages}
        return search_cache[cache_key]

    scrape_progress[job_id] = {"current": 0, "total": max_pages, "status": "initializing"}
    
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.page_load_strategy = 'eager'
    
    prefs = {'profile.managed_default_content_settings': {'images': 2, 'plugins': 2, 'media_stream': 2}}
    options.add_experimental_option('prefs', prefs)
    options.add_argument('--disable-logging')
    options.add_argument('--log-level=3')

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.implicitly_wait(3)
    wait = WebDriverWait(driver, 5) # 최대 5초 대기
    
    restaurant_list = []
    try:
        driver.get("https://map.kakao.com/")
        
        search_box = driver.find_element(By.ID, "search.keyword.query")
        search_box.send_keys(region_query)
        search_box.send_keys(Keys.ENTER)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.PlaceItem"))) # 검색 결과가 뜰 때까지 대기
        
        try:
            more_button = driver.find_element(By.ID, "info.search.place.more")
            driver.execute_script("arguments[0].click();", more_button)
            wait.until(EC.visibility_of_element_located((By.ID, "info.search.page"))) # 페이지 번호가 보일 때까지 대기
        except:
            pass

        scrape_progress[job_id]["status"] = "scraping"
        
        for page in range(1, max_pages + 1):
            scrape_progress[job_id]["current"] = page
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.PlaceItem")))
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            places = soup.select("li.PlaceItem")
            
            for place in places:
                try:
                    name = place.select_one("a.link_name").text.strip()
                    category = place.select_one("span.subcategory").text.strip() if place.select_one("span.subcategory") else ""
                    link = place.select_one("a.moreview").get('href', '#')
                    
                    rating_tag = place.select_one("em.num")
                    rating = float(rating_tag.text) if rating_tag and rating_tag.text != '0.0' else 0.0
                    
                    rating_count_tag = place.select_one("a[data-id='numberofscore']") or place.select_one(".rating .numberofscore")
                    rating_count_str = rating_count_tag.text.strip() if rating_count_tag else "0"
                    rating_count = int(re.sub(r'[^0-9]', '', rating_count_str))
                    
                    address = place.select_one("div.info_item > div.addr > p").text.strip()
                    
                    restaurant_list.append({"상호명": name, "업종": category, "평점": rating, "후기수": rating_count, "주소": address, "링크": link})
                except:
                    continue
                    
            if page == max_pages: break
            
            # 다음 페이지로 넘어가기 전 현재 목록의 첫 번째 아이템을 기억해둠
            first_item = driver.find_element(By.CSS_SELECTOR, "li.PlaceItem")
            try:
                next_page_num = page + 1
                if next_page_num % 5 == 1:
                    driver.execute_script("arguments[0].click();", driver.find_element(By.ID, "info.search.page.next"))
                else:
                    page_btn = driver.find_element(By.ID, f"info.search.page.no{next_page_num % 5 if next_page_num % 5 != 0 else 5}")
                    driver.execute_script("arguments[0].click();", page_btn)
                wait.until(EC.staleness_of(first_item)) # 이전 목록의 아이템이 사라질 때까지(DOM 업데이트 완료) 대기
            except:
                break
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
                    df = df.drop_duplicates(subset=['상호명'])
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
    app.run(host='0.0.0.0', port=5000, threaded=True)