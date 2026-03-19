import time
import re
import uuid
import urllib.parse
import pandas as pd
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, render_template_string, jsonify
from cachetools import TTLCache
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

app = Flask(__name__)

# --- 💡 로깅 설정 ---
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
    
    driver.get("https://map.kakao.com/")
    
    search_box = driver.find_element(By.ID, "search.keyword.query")
    search_box.send_keys(region_query)
    search_box.send_keys(Keys.ENTER)
    time.sleep(1)
    
    try:
        more_button = driver.find_element(By.ID, "info.search.place.more")
        driver.execute_script("arguments[0].click();", more_button)
        time.sleep(0.5)
    except:
        pass

    restaurant_list = []
    scrape_progress[job_id]["status"] = "scraping"
    
    for page in range(1, max_pages + 1):
        scrape_progress[job_id]["current"] = page
        time.sleep(0.5)
        
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
        
        try:
            next_page_num = page + 1
            if next_page_num % 5 == 1:
                driver.execute_script("arguments[0].click();", driver.find_element(By.ID, "info.search.page.next"))
            else:
                page_btn = driver.find_element(By.ID, f"info.search.page.no{next_page_num % 5 if next_page_num % 5 != 0 else 5}")
                driver.execute_script("arguments[0].click();", page_btn)
        except:
            break

    driver.quit()
    search_cache[cache_key] = restaurant_list
    return restaurant_list

# --- 💡 웹 UI 화면 ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>카카오맵 맛집 크롤러</title>
    <style>
        body { font-family: 'Malgun Gothic', sans-serif; background-color: #f4f5f6; padding: 20px; margin: 0; color: #333; }
        .container { max-width: 1000px; margin: 0 auto; background-color: #ffffff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }
        h1 { text-align: center; color: #1e1e1e; font-size: 1.8em; margin-bottom: 30px; }
        
        /* 폼 박스 스타일 */
        .form-box { background-color: #fcfcfc; padding: 30px; border-radius: 10px; border: 1px solid #eaeaea; margin-bottom: 30px; }
        .input-group { margin-bottom: 20px; text-align: left; max-width: 500px; margin-left: auto; margin-right: auto; }
        .input-group label { display: block; font-weight: bold; margin-bottom: 8px; color: #2c3e50; font-size: 1.05em; }
        .input-group input[type="text"], .input-group input[type="number"] { width: 100%; padding: 12px; border: 1px solid #ccd1d9; border-radius: 6px; font-size: 14px; box-sizing: border-box; transition: border-color 0.3s; }
        .input-group input:focus { border-color: #f1c40f; outline: none; }
        
        /* 💡 버튼: 카카오 노란색 복구 */
        .submit-btn-wrapper { text-align: center; margin-top: 30px; }
        input[type="submit"] { padding: 14px 30px; background-color: #FAE100; color: #1e1e1e; border: none; border-radius: 6px; font-size: 16px; font-weight: bold; cursor: pointer; width: 100%; max-width: 500px; transition: background-color 0.2s; }
        input[type="submit"]:hover { background-color: #f1c40f; }
        
        /* 테이블 래퍼 및 스타일 */
        .table-wrapper { box-shadow: 0 2px 10px rgba(0,0,0,0.08); border-radius: 8px; overflow: hidden; margin-top: 20px; }
        .table-responsive { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; font-size: 0.95em; min-width: 750px; background-color: #ffffff; text-align: center; }
        
        /* 💡 테이블 헤더: 카카오 노란색 복구 */
        th { background-color: #FAE100; color: #1e1e1e; font-weight: bold; padding: 15px 10px; white-space: nowrap; letter-spacing: 0.5px; border-bottom: 2px solid #e5c100; }
        td { padding: 14px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }
        
        /* 제브라 패턴 및 마우스 오버 효과 */
        tbody tr:nth-child(even) { background-color: #fafbfc; }
        tbody tr:hover { background-color: #f1f4f8; transition: background-color 0.2s ease; }
        
        /* 열별 개별 정렬 */
        th:nth-child(2), td:nth-child(2) { text-align: left; font-size: 1.05em; font-weight: bold; }
        th:nth-child(6), td:nth-child(6) { text-align: left; font-size: 0.9em; }
        
        /* 링크 및 상호작용 요소 */
        table a { color: #2980b9; text-decoration: none; }
        table a:hover { color: #e74c3c; text-decoration: underline; }
        .clickable-category { cursor: pointer; color: #d35400; font-weight: 600; padding: 4px 8px; background-color: #fdf3e7; border-radius: 4px; transition: all 0.2s; }
        .clickable-category:hover { background-color: #e67e22; color: #ffffff; }

        /* 로딩 타이머 및 광고 */
        .loading { display: none; text-align: center; padding: 20px; background-color: #fdfbf7; border-radius: 8px; margin-top: 20px; border: 1px dashed #f1c40f; }
        .live-timer { color: #e74c3c; font-size: 1.2em; font-weight: bold; }
        .progress-text { color: #2980b9; font-weight: bold; font-size: 1.1em; display: block; margin-bottom: 5px; }
        .ad-banner { background-color: #f8f9fa; border: 1px dashed #bdc3c7; padding: 20px; text-align: center; color: #95a5a6; margin: 25px 0; border-radius: 8px; font-size: 0.9em; }
    </style>
    <script>
        let jobId = "{{ job_id }}";
        let intervalId;
        
        function startLoadingTimer() {
            document.getElementById('loading').style.display = 'block';
            let startTime = Date.now();
            
            setInterval(() => { 
                document.getElementById('live-time').innerText = Math.floor((Date.now() - startTime) / 1000); 
            }, 1000);
            
            intervalId = setInterval(() => {
                fetch('/progress/' + jobId)
                .then(res => res.json())
                .then(data => {
                    let statusText = document.getElementById('status-text');
                    if (data.status === "cached") {
                        statusText.innerHTML = "저장된 데이터를 불러오는 중입니다 ⚡";
                    } else if (data.status === "initializing") {
                        statusText.innerHTML = "초기 브라우저 구동 및 검색어 입력 중 🚀";
                    } else if (data.status === "scraping") {
                        statusText.innerHTML = "현재 " + data.total + "페이지 중 " + data.current + "페이지 탐색 중 🚀";
                    }
                });
            }, 1000);
        }

        function addExcludeWord(word) {
            let input = document.getElementById('exclude-words-input');
            let words = input.value.split(',').map(w => w.trim()).filter(w => w.length > 0);
            if (!words.includes(word)) {
                words.push(word);
                input.value = words.join(', ');
                input.style.backgroundColor = "#fff9c4";
                setTimeout(() => { input.style.backgroundColor = ""; }, 800);
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
        }
    </script>
</head>
<body>
    <div class="container">
        <h1>🔍 카카오맵 찐맛집 데이터 추출기</h1>
        <div class="ad-banner">[구글 애드센스 상단 광고 영역]</div>
        
        <div class="form-box">
            <form method="POST" onsubmit="startLoadingTimer();">
                <input type="hidden" name="job_id" value="{{ job_id }}">
                
                <div class="input-group">
                    <label for="query">📍 검색어 (필수)</label>
                    <input type="text" id="query" name="query" value="{{ query }}" placeholder="지역명과 식당 종류 입력 (예: 강남역 맛집)" required>
                </div>

                <div class="input-group">
                    <label for="max_pages">📑 탐색할 페이지 수</label>
                    <input type="number" id="max_pages" name="max_pages" value="{{ max_pages }}" min="1" max="34" placeholder="최대 34 (숫자만 입력)">
                </div>

                <div class="input-group">
                    <label for="exclude-words-input">🚫 제외할 업종 (선택)</label>
                    <input type="text" id="exclude-words-input" name="exclude_words" value="{{ exclude_words }}" placeholder="쉼표로 구분 또는 아래 목록의 [업종] 클릭">
                </div>

                <div class="submit-btn-wrapper">
                    <input type="submit" value="🚀 데이터 추출 시작">
                </div>
            </form>
            
            <div id="loading" class="loading">
                <span class="progress-text" id="status-text">서버 연결 중...</span>
                <span class="live-timer"><span id="live-time">0</span>초</span> 경과
                <div style="font-size: 0.85em; color: #7f8c8d; margin-top: 8px;">데이터 양에 따라 10초 ~ 1분 정도 소요될 수 있습니다.</div>
            </div>
        </div>

        {% if table_html %}
            <h3 style="text-align:center; color: #2c3e50; margin-top: 40px;">🏆 '{{ query }}' 평점 4.0 이상 랭킹</h3>
            <p style="text-align:center; color:#27ae60; font-weight:bold; margin-bottom: 20px;">⏱️ 총 소요 시간: {{ elapsed_time }}초 (캐시 적용 여부에 따라 단축됨)</p>
            <div class="table-wrapper">
                <div class="table-responsive">
                    {{ table_html | safe }}
                </div>
            </div>
            <div class="ad-banner">[구글 애드센스 하단 광고 영역]</div>
        {% endif %}
    </div>
</body>
</html>
"""

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
            data = crawl_kakao_map(query, max_pages, current_job_id)
            df = pd.DataFrame(data)
            
            num_res = 0
            if not df.empty:
                df = df.drop_duplicates(subset=['상호명'])
                if exclude_words.strip():
                    pattern = '|'.join([w.strip() for w in exclude_words.split(',') if w.strip()])
                    df = df[~df['업종'].str.contains(pattern, na=False)]
                
                final_df = df[df['평점'] >= 4.0].sort_values(by='후기수', ascending=False).head(10)
                num_res = len(final_df)
                
                if not final_df.empty:
                    final_df.insert(0, '순위', range(1, num_res + 1))
                    
                    final_df['상호명'] = '<a href="' + final_df['링크'] + '" target="_blank">' + final_df['상호명'] + '</a>'
                    final_df['업종'] = '<span class="clickable-category" onclick="addExcludeWord(\'' + final_df['업종'] + '\')" title="클릭하여 제외 업종에 추가">' + final_df['업종'] + '</span>'
                    
                    # 💡 주소 텍스트를 카카오맵 길찾기(도착지 설정) 링크로 변환 (기존 코드 유지)
                    final_df['주소'] = final_df['주소'].apply(
                        lambda x: f'<a href="https://map.kakao.com/?eName={urllib.parse.quote(x)}" target="_blank" title="카카오맵 길찾기로 이동" style="color:#2c3e50; font-weight:500; text-decoration:underline;">{x}</a>'
                    )
                    
                    table_html = final_df[['순위', '상호명', '업종', '평점', '후기수', '주소']].to_html(escape=False, index=False, border=0)
            
            elapsed_time = f"{time.time() - start_t:.2f}"
            
            try: logger.info(f"IP:{user_ip}|Q:'{query}'|P:{max_pages}|R:{num_res}|T:{elapsed_time}s")
            except: pass

            if current_job_id in scrape_progress:
                del scrape_progress[current_job_id]

    new_job_id = str(uuid.uuid4())
    return render_template_string(HTML_TEMPLATE, table_html=table_html, query=query, max_pages=max_pages, exclude_words=exclude_words, elapsed_time=elapsed_time, job_id=new_job_id)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)