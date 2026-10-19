import os
import json
import time
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.pbc.gov.cn"
# 人民银行没有 RSS 源，只能解析栏目列表页；HTTP 请求会返回 502，必须走 HTTPS
MPC_LIST_URL = BASE_URL + "/zhengcehuobisi/125207/3870933/3870936/index.html"
NEWS_LIST_URL = BASE_URL + "/goutongjiaoliu/113456/113469/index.html"
# 新闻发布栏目很杂（会见、出席会议等），只保留标题命中以下关键词的政策动作公告
ACTION_KEYWORDS = ["降准", "降息", "存款准备金率", "政策利率", "逆回购", "贷款市场报价利率", "LPR", "货币政策"]

SOURCES = [
    {"name": "例会", "url": MPC_LIST_URL, "kind": "meeting"},
    {"name": "新闻发布", "url": NEWS_LIST_URL, "kind": "action"},
]

DATA_DIR = "data"
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
STATE_FILE = os.path.join(DATA_DIR, "last_seen.json")
LATEST_TEXT_FILE = os.path.join(DATA_DIR, "latest_policy.txt")
PREV_TEXT_FILE = os.path.join(DATA_DIR, "previous_policy.txt")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
RETRIES = 3
REQUEST_INTERVAL = 1

def get(url):
    """带重试的 GET；官网偶发 502，失败后递增等待再试"""
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            time.sleep(REQUEST_INTERVAL)
            return resp
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(attempt * 3)

def fetch_list(source):
    """解析栏目列表页，返回 [{title, link, date, kind}]"""
    soup = BeautifulSoup(get(source["url"]).content, "html.parser")
    items = []
    for a in soup.find_all("a", attrs={"istitle": "true"}):
        cell = a.find_parent("td")
        date_span = cell.find("span", class_="hui12") if cell else None
        if not date_span:
            continue
        # 显示文字会被截断，完整标题在 title 属性里
        title = (a.get("title") or a.get_text()).strip()
        items.append({
            "title": title,
            "link": BASE_URL + a["href"],
            "date": date_span.get_text(strip=True),
            "kind": source["kind"],
        })
    return items

def fetch_article_body(url):
    soup = BeautifulSoup(get(url).content, "html.parser")
    body = soup.find("div", id="zoom") or soup.find("td", class_="content")
    if body:
        return body.get_text(separator="\n", strip=True)
    return "\n".join(p.get_text(strip=True) for p in soup.find_all("p"))

def matched_keywords(title):
    return [kw for kw in ACTION_KEYWORDS if kw in title]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_links": []}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def update_latest_meeting(record):
    """新例会新闻稿：旧的 latest_policy.txt 备份为 previous_policy.txt，供 AI 做措辞边际对比"""
    if os.path.exists(LATEST_TEXT_FILE):
        with open(LATEST_TEXT_FILE, "r", encoding="utf-8") as f:
            old_content = f.read()
        with open(PREV_TEXT_FILE, "w", encoding="utf-8") as f:
            f.write(old_content)
    with open(LATEST_TEXT_FILE, "w", encoding="utf-8") as f:
        f.write(record)

def main():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    
    print("正在连接人民银行官网检查最新公告...")
    candidates = []
    for source in SOURCES:
        for it in fetch_list(source):
            if it["kind"] == "action":
                it["keywords"] = matched_keywords(it["title"])
                if not it["keywords"]:
                    continue
            candidates.append(it)
    
    # 网址编号不按时间递增，不能用编号或时间截止点判断新旧，只按链接去重；
    # 新闻栏目若转载了例会稿，按标题去重并保留例会栏目那条
    state = load_state()
    seen = set(state["seen_links"])
    meeting_titles = {it["title"] for it in candidates if it["kind"] == "meeting"}
    new_items, picked = [], set()
    for it in candidates:
        if it["link"] in seen or it["link"] in picked:
            continue
        if it["kind"] == "action" and it["title"] in meeting_titles:
            continue
        new_items.append(it)
        picked.add(it["link"])
    
    if not new_items:
        print("[无更新] 例会信息与新闻发布栏目均无新的货币政策公告")
        return
    
    # 按列表页日期从旧到新处理，同一天例会排在政策动作之前，保证 latest/previous 顺序正确
    new_items.sort(key=lambda it: (it["date"], it["kind"] != "meeting"))
    print(f"[发现 {len(new_items)} 条新公告]")
    for it in new_items:
        print(f"正在抓取：{it['date']} | {it['title']}")
        body_text = fetch_article_body(it["link"])
        record = f"标题: {it['title']}\n发布时间: {it['date']}\n原文链接: {it['link']}\n\n--- 正文内容 ---\n{body_text}"
        
        link_id = it["link"].rstrip("/").split("/")[-2]
        archive_name = f"{it['date']}_{link_id}.txt"
        with open(os.path.join(ARCHIVE_DIR, archive_name), "w", encoding="utf-8") as f:
            f.write(record)
        
        if it["kind"] == "meeting":
            update_latest_meeting(record)
            print("  -> 新例会新闻稿，已更新 data/latest_policy.txt")
        else:
            print(f"  -> 政策动作公告（关键词：{'、'.join(it['keywords'])}），存档：data/archive/{archive_name}")
        
        # 每抓完一条就保存进度，中途失败时下次可从断点继续
        state["seen_links"].append(it["link"])
        save_state(state)
    
    print(f"抓取完成！全部公告已存档至 {ARCHIVE_DIR}")

if __name__ == "__main__":
    main()
