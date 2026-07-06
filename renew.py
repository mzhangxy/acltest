#!/usr/bin/env python3
"""
ACLClouds MC账号 专用续期脚本 (SeleniumBase UC 模式终极版)
- 启用 UC 模式 (Undetected ChromeDriver) 抹除底层自动化指纹
- 引入 3.5~5 秒的人类思考时间伪装，绕过时间差风控
- 取消网络并发请求，交还网页自然发包，通过 UI 实质刷新判定结果
"""

import os
import re
import sys
import json
import time
import traceback
import io
import random
import requests
from urllib.request import Request, urlopen
from difflib import SequenceMatcher

try:
    from seleniumbase import SB
    from selenium.webdriver.common.action_chains import ActionChains
    from PIL import Image
    import pytesseract
except ImportError:
    print("[ERROR] 缺少核心库，请执行: pip install seleniumbase pytesseract pillow requests")
    sys.exit(1)

# ── 环境变量配置 ──────────────────────────────────────────
PROXY_SERVER = os.environ.get("MC_PROXY", "").strip()
EMAIL        = os.environ.get("MC_EMAIL", "").strip()
PASSWORD     = os.environ.get("MC_PASSWORD", "").strip()
MC_COOKIE    = os.environ.get("MC_COOKIE", "").strip()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "").strip()

RENEW_THRESHOLD_DAYS = 2 / 24   # 剩余 < 2小时 续期

BASE_URL  = "https://dash.aclclouds.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

def log(msg):       print(f"[INFO] {msg}", flush=True)
def log_warn(msg):  print(f"[WARN] {msg}", flush=True)
def log_error(msg): print(f"[ERROR] {msg}", flush=True)

def mask_email(email: str) -> str:
    if not email or "@" not in email: return "***"
    local, domain = email.split("@", 1)
    return f"{local[0]}**@{domain[0]}***" if len(local)>1 else f"**@{domain[0]}***"

def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    try:
        body = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req  = Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                       data=body, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=15)
        log("TG 推送成功")
    except Exception as e:
        log_warn(f"TG 推送失败: {e}")

def parse_expires(text):
    if text is None: return None
    s = str(text).strip()
    if re.search(r'\d{4}-\d{2}-\d{2}', s):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except: pass
    try: return float(s) / 86400
    except: pass
    
    sl, days, hours, minutes = s.lower(), 0.0, 0.0, 0.0
    m = re.search(r'(\d+(?:\.\d+)?)\s*[dj]', sl)
    if m: days = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*h', sl)
    if m: hours = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*m(?!o)', sl)
    if m: minutes = float(m.group(1))
    
    total = days + hours / 24 + minutes / 1440
    return total if total > 0 else None

def fmt_remaining(days: float) -> str:
    h, m = divmod(int(days * 24 * 60), 60)
    return f"{h}h {m}min" if m else f"{h}h"

# ── API 接口调用 (使用纯 Python Requests 以隔离 UI 干扰) ──────────────────
def fetch_api(sb, endpoint: str, method="GET", body=None):
    url = f"{BASE_URL}{endpoint}"
    cookies_dict = {c['name']: c['value'] for c in sb.driver.get_cookies()}
    xsrf = cookies_dict.get('XSRF-TOKEN', '')
    
    from urllib.parse import unquote
    headers = {
        'Accept': 'application/json',
        'X-XSRF-TOKEN': unquote(xsrf),
        'User-Agent': sb.driver.execute_script("return navigator.userAgent;")
    }
    
    proxies = None
    if PROXY_SERVER:
        proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER}

    if body:
        res = requests.request(method, url, json=body, cookies=cookies_dict, headers=headers, proxies=proxies, timeout=15)
    else:
        res = requests.request(method, url, cookies=cookies_dict, headers=headers, proxies=proxies, timeout=15)
    
    return {"status": res.status_code, "body": res.text}

def check_server_online(sb, identifier: str):
    res = fetch_api(sb, f"/api/client/servers/{identifier}/resources")
    if res['status'] != 200:
        res2 = fetch_api(sb, f"/api/client/servers/{identifier}")
        if res2['status'] != 200: return None
        return False if json.loads(res2['body']).get('attributes', {}).get('suspended', False) else None
    
    attrs = json.loads(res['body']).get('attributes', {})
    state = attrs.get('current_state', 'unknown')
    if attrs.get('is_suspended', False): return False
    if state in ('running', 'starting'): return True
    if state in ('offline', 'stopping', 'stopped'): return False
    return None

def start_server(sb, identifier: str) -> bool:
    res = fetch_api(sb, f"/api/client/servers/{identifier}/power", "POST", {"signal": "start"})
    return res['status'] in (200, 204)

def wait_until_running(sb, identifier: str, max_wait: int = 120, interval: int = 10) -> bool:
    for elapsed in range(0, max_wait, interval):
        time.sleep(interval)
        res = fetch_api(sb, f"/api/client/servers/{identifier}/resources")
        state = json.loads(res['body']).get('attributes', {}).get('current_state', 'unknown') if res['status'] == 200 else 'unknown'
        log(f"  等待启动中... {elapsed+interval}s / {max_wait}s，当前状态: {state!r}")
        if state == 'running': return True
    return False

# ── 主流程 ────────────────────────────────────────────────
def run():
    log(f"账号: {mask_email(EMAIL)}")
    log(f"续期阈值: < {RENEW_THRESHOLD_DAYS*24:.1f} 小时")

    renewed_list, offline_list, skipped_list, failed_list = [], [], [], []

    # 启用 UC 模式 (Undetected ChromeDriver)，如果在 Linux 上运行则必须开启 xvfb
    with SB(uc=True, xvfb=True, proxy=PROXY_SERVER, locale_code="en-US") as sb:
        
        try:
            login_success = False
            if MC_COOKIE:
                log("尝试使用 Cookie 访问仪表盘...")
                sb.open(BASE_URL)
                sb.sleep(2)
                try:
                    cookies = json.loads(MC_COOKIE)
                    if isinstance(cookies, dict): cookies = [cookies]
                    for c in cookies:
                        sb.driver.add_cookie(c)
                except json.JSONDecodeError:
                    for pair in MC_COOKIE.split(';'):
                        if '=' in pair:
                            k, v = pair.split('=', 1)
                            sb.driver.add_cookie({"name": k.strip(), "value": v.strip(), "domain": "dash.aclclouds.com", "path": "/"})
                
                sb.open(BASE_URL)
                sb.sleep(3)
                if "login" not in sb.get_current_url():
                    log(f"✅ Cookie 登录成功!")
                    login_success = True
                else:
                    log_warn("⚠️ Cookie 已失效或不完整，降级为账密登录...")

            if not login_success:
                log(f"账密登录: {LOGIN_URL}")
                sb.open(LOGIN_URL)
                
                sb.type("input[type='email']", EMAIL)
                sb.type("input[type='password']", PASSWORD)

                captcha_ok = False
                for attempt in range(1, 4):
                    try: 
                        sb.click("div.auth-captcha-inner", timeout=5)
                    except: pass
                    
                    try:
                        # 循环检测文本变化，比单一选择器更稳定
                        for _ in range(30):
                            if "Verified" in sb.get_text("div.auth-captcha-box") or "verified" in sb.get_text("div.auth-captcha-box"):
                                log("captcha 验证通过 ✅")
                                captcha_ok = True
                                break
                            sb.sleep(0.5)
                        if captcha_ok: break
                    except:
                        log_warn(f"captcha 第 {attempt} 次等待超时")
                
                if not captcha_ok:
                    raise RuntimeError("captcha 验证失败，放弃登录")

                sb.click("button[type='submit']")
                sb.sleep(3)
                if "login" in sb.get_current_url():
                    raise RuntimeError("登录提交超时或被拒绝")
                log(f"账密登录成功 ✅")

            target_url = f"{BASE_URL}/projects"
            if sb.get_current_url() != target_url:
                sb.open(target_url)
                sb.sleep(3)

            res = fetch_api(sb, "/api/client")
            if res['status'] != 200:
                log_warn("面板接口异常，视为无项目")
                return renewed_list, offline_list, skipped_list, failed_list

            projects = [i['attributes'] for i in json.loads(res['body']).get('data', []) if i.get('attributes')]
            log(f"找到 {len(projects)} 个项目")
                
            for project in projects:
                name, identifier = project.get("name", "未知"), project.get("identifier", "")
                remaining = parse_expires(project.get("expires_at"))
                log(f"\n── 项目: {name} ──")

                if remaining is None:
                    failed_list.append(f"{name} (解析时间失败)")
                    continue

                remaining_str = fmt_remaining(remaining)
                log(f"  剩余时间: {remaining_str}")

                if remaining >= RENEW_THRESHOLD_DAYS:
                    skipped_list.append(f"{name} ({remaining_str})")
                    log("  时间充足，跳过续期。")
                else:
                    log("  尝试通过 UI 模拟点击触发 403 挑战...")
                    try:
                        card_xpath = f"//div[contains(@class, 'client-card') and contains(., '{name}')]"
                        if not sb.is_element_visible(card_xpath):
                            log_warn(f"  ⚠️ 找不到操作按钮卡片，跳过。")
                            failed_list.append(f"{name} (UI找不到卡片)")
                            continue
                        
                        btn_xpath = f"{card_xpath}//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'renew') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'renouveler') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reactivate') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'réactiver')]"
                        
                        if not sb.is_element_visible(btn_xpath):
                            log_warn("  ⚠️ 找不到操作按钮，跳过。")
                            failed_list.append(f"{name} (无按钮)")
                            continue
                        
                        btn_text = sb.get_text(btn_xpath).strip() or "续期/激活"
                        is_activation = "activ" in btn_text.lower()
                        log(f"  发现并点击 [{btn_text}] 按钮...")
                        sb.click(btn_xpath)
                        
                        log("  等待服务器下发验证码挑战...")
                        need_captcha = False
                        sb.sleep(2)
                        
                        if sb.is_element_visible("div.auth-captcha-inner"):
                            need_captcha = True
                        else:
                            log("  ✅ 未检测到验证码弹窗，可能触发了免验证通道...")
                            
                        if need_captcha:
                            log("  出现验证码，开始处理...")
                            sb.click("div.auth-captcha-inner")
                            
                            solved = False
                            for _ in range(30):
                                if not sb.is_element_visible("div[role='dialog']"):
                                    solved = True
                                    log("  captcha 弹窗已自动关闭...")
                                    break
                                    
                                box_text = sb.get_text("div.auth-captcha-box")
                                if "Verified" in box_text or "verified" in box_text:
                                    solved = True
                                    log("  captcha 状态变为验证通过 ✅")
                                    break
                                    
                                dialog_text = sb.get_text("div[role='dialog']")
                                m = re.search(r'(?:Cliquez sur|Click on)\s+([A-Za-z0-9_]+)', dialog_text, re.IGNORECASE)
                                target_word = m.group(1).strip() if m else ""
                                        
                                if target_word:
                                    log(f"  触发二次验证，系统要求点击: [{target_word}]")
                                    clicked = False
                                    options = sb.find_elements(".auth-captcha-option")
                                    
                                    for btn_loc in options:
                                        if not btn_loc.is_displayed(): continue
                                        
                                        try:
                                            img_bytes = btn_loc.find_element("css selector", "img").screenshot_as_png
                                            img = Image.open(io.BytesIO(img_bytes)).convert('L')
                                            img = img.point(lambda p: 255 if p > 150 else 0)
                                            ocr_text = pytesseract.image_to_string(img, config='--psm 7').strip()
                                            
                                            clean_ocr = re.sub(r'[^a-zA-Z0-9]', '', ocr_text).lower()
                                            clean_target = re.sub(r'[^a-zA-Z0-9]', '', target_word).lower()
                                            similarity = SequenceMatcher(None, clean_target, clean_ocr).ratio()
                                            
                                            if clean_target and clean_ocr and (clean_target in clean_ocr or similarity > 0.6):
                                                log(f"  🎯 成功锁定目标选项！")
                                                
                                                fake_think_time = random.uniform(3.5, 5.0)
                                                log(f"  ⏳ 模拟人类识别停顿，休眠 {fake_think_time:.2f} 秒以绕过时间差风控...")
                                                sb.sleep(fake_think_time)
                                                
                                                w = btn_loc.size['width']
                                                h = btn_loc.size['height']
                                                x_off = random.uniform(-w/4, w/4)
                                                y_off = random.uniform(-h/4, h/4)
                                                
                                                actions = ActionChains(sb.driver)
                                                actions.move_to_element_with_offset(btn_loc, x_off, y_off)
                                                actions.pause(random.uniform(0.1, 0.2)).click().perform()
                                                
                                                log(f"  👆 已模拟真人轨迹偏移点击")
                                                clicked = True
                                                sb.sleep(1)
                                                break
                                        except Exception as ocr_e:
                                            log_warn(f"  ⚠️ OCR 处理异常: {ocr_e}")
                                    
                                    if not clicked: sb.sleep(1)
                                else:
                                    sb.sleep(1)
                            
                            if not solved:
                                raise Exception("验证码超时或未能解决二次选择挑战")
                            
                        log("  验证通过，将发包权交还给网页前端，等待 UI 状态实质性刷新 (最多 20 秒)...")
                        state_changed = False
                        
                        for _ in range(40):
                            if is_activation:
                                if "Suspendu" not in sb.get_text(card_xpath) and "Suspended" not in sb.get_text(card_xpath):
                                    state_changed = True
                                    break
                            else:
                                card_text = sb.get_text(card_xpath)
                                expire_idx = card_text.lower().find("expire")
                                if expire_idx != -1:
                                    new_remaining = parse_expires(card_text[expire_idx:])
                                    if new_remaining is not None and new_remaining > remaining + 0.001:
                                        state_changed = True
                                        log(f"  ✅ 监测到时间实质性增加: 刷新后约 {fmt_remaining(new_remaining)}")
                                        break
                            sb.sleep(0.5)
                            
                        if not state_changed:
                            # 捕获可能的遗留前台报错
                            page_text = sb.get_text("body")
                            if "Trop de tentatives" in page_text or "Too many" in page_text:
                                raise Exception("前端抛出风控频率限制报错 (429)。")
                            else:
                                raise Exception("操作已执行，但 20 秒内页面状态未发生实质性改变。")

                        action_type = "重新激活" if is_activation else "续期"
                        renewed_list.append(f"{name} ({action_type}成功, 前状态: {remaining_str})")
                        log(f"  UI {action_type}流程执行完毕 ✅")
                        sb.sleep(3)

                    except Exception as e:
                        log_warn(f"  ❌ 交互/发包失败: {e}")
                        failed_list.append(f"{name} (失败: {str(e)[:25]})")
                        continue

                online = check_server_online(sb, identifier)
                if online is False:
                    log_warn("  ❌ 发现服务离线，尝试发送启动信号...")
                    if start_server(sb, identifier):
                        if wait_until_running(sb, identifier): 
                            log("  ✅ 成功启动！")
                        else: 
                            log_warn("  ⚠️ 启动信号已发送，但未能在规定时间内监测到运行状态。")
                            offline_list.append(name)
                    else: 
                        log_warn("  ❌ 启动请求被面板拒绝。")
                        offline_list.append(name)
                elif online is True: 
                    log("  ✅ 服务状态正常 (在线)")
                else:
                    log_warn("  ⚠️ 无法获取确切的在线状态。")

        except Exception as e:
            send_tg(f"❌ <b>ACLClouds 脚本异常</b>\n\n<code>{str(e)[:200]}</code>")
            raise

    return renewed_list, offline_list, skipped_list, failed_list

if __name__ == "__main__":
    if not EMAIL or not PASSWORD:
        log_error("缺少 MC_EMAIL 或 MC_PASSWORD")
        sys.exit(1)

    try:
        renewed_list, offline_list, skipped_list, failed_list = run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    if offline_list:
        send_tg("🚨 <b>ACLClouds 服务离线</b>\n" + "\n".join(f"• {n}" for n in offline_list))

    if renewed_list or failed_list:
        lines = []
        if renewed_list: lines += ["✅ <b>ACLClouds 续期成功</b>"] + [f"• {i}" for i in renewed_list]
        if failed_list: lines += ["\n❌ <b>ACLClouds 失败项目</b>"] + [f"• {i}" for i in failed_list]
        if skipped_list: lines += ["\n⏳ <b>ACLClouds 未到窗口</b>"] + [f"• {i}" for i in skipped_list]
        send_tg("\n".join(lines))
