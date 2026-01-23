#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClawCloud 多账号自动保活脚本 - Selenium 版本
适配 Ubuntu 22.04 ARM64 平台
支持多账号、Cookie复用、2FA自动验证、Telegram 通知
"""

import os
import sys
import time
import json
import requests
import re
import pyotp
from datetime import datetime
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ============ 配置区域 ============

# 方式1: 从环境变量读取(推荐)
# export CLAW_ACCOUNTS="账号1----密码1----2FA密钥1&账号2----密码2----2FA密钥2"

# 方式2: 脚本内配置(备用)
ACCOUNTS_CONFIG = [
    {
        "username": "56959566@qq.com",
        "password": "windowsme198400",
        "totp_secret": "CJ3FIB2LNREKBF5G"
    },
    {
        "username": "adjlevis@qq.com",
        "password": "windowsme198400",
        "totp_secret": "YSXWVNVMDBRAGPEQ"
    }
]

def load_accounts_from_env():
    """从环境变量或脚本配置加载账号"""
    accounts = []
    
    # 优先从环境变量读取
    env_accounts = os.environ.get("CLAW_ACCOUNTS", "").strip()
    
    if env_accounts:
        logger.info("✅ 从环境变量 CLAW_ACCOUNTS 加载账号配置")
        for acc_str in env_accounts.split("&"):
            parts = acc_str.strip().split("----")
            if len(parts) >= 2:
                account = {
                    "username": parts[0].strip(),
                    "password": parts[1].strip(),
                    "totp_secret": parts[2].strip() if len(parts) > 2 else ""
                }
                accounts.append(account)
                logger.info(f"  - 加载账号: {account['username']}")
    
    # 如果环境变量为空,使用脚本配置
    if not accounts and ACCOUNTS_CONFIG:
        logger.info("⚠️ 环境变量未配置,使用脚本中的账号配置")
        accounts = ACCOUNTS_CONFIG
        for idx, acc in enumerate(accounts, 1):
            logger.info(f"  - 账号 {idx}: {acc['username']}")
    
    return accounts

ACCOUNTS = load_accounts_from_env()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
CLAW_CLOUD_URL = os.environ.get("CLAW_CLOUD_URL", "https://eu-central-1.run.claw.cloud").strip()

# 脚本目录 - Ubuntu 路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) or "/tmp"
# ================================


class Telegram:
    """Telegram 通知类"""
    
    def __init__(self):
        self.token = TG_BOT_TOKEN
        self.chat_id = int(TG_CHAT_ID) if TG_CHAT_ID and TG_CHAT_ID.isdigit() else None
        self.ok = bool(self.token and self.chat_id and self.token != "your_tg_bot_token")

    def send(self, msg):
        """发送 TG 消息"""
        if not self.ok:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=30
            )
        except Exception as e:
            logger.warning(f"TG 消息发送失败: {e}")

    def photo(self, path, caption=""):
        """发送 TG 图片"""
        if not self.ok or not os.path.exists(path):
            return None
        try:
            with open(path, 'rb') as f:
                resp = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": f},
                    timeout=60
                )
                if resp.ok:
                    return resp.json().get("result", {}).get("message_id")
        except Exception as e:
            logger.warning(f"TG 图片发送失败: {e}")
        return None


class AutoLogin:
    """ClawCloud 自动登录和保活类"""
    
    def __init__(self, account, account_index):
        self.logs = []
        self.shots = []
        self.n = 0
        self.used_old_cookie = False
        self.authenticator_2fa = False
        self.github_mobile_2fa = False
        self.username = account["username"]
        self.password = account["password"]
        self.totp_secret = account.get("totp_secret", "").strip()
        self.account_index = account_index
        self.cookie_file = os.path.join(
            SCRIPT_DIR,
            f"cookies_{self.username.replace('@', '_').replace('.', '_')}.json"
        )
        self.tg = Telegram()
        self.old_cookies = self.load_cookies()
        self.balance = "未知"
        self.success = True
        self.notify_content = ""
        self.driver = None

    def log(self, msg, level="INFO"):
        """记录日志"""
        icons = {"INFO": "😲", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "😃"}
        line = f"{icons.get(level, '•')} [{self.username}] {msg}"
        logger.info(line)
        self.logs.append(msg)

    def shot(self, name, push_to_tg=False, caption=""):
        """截图"""
        if not (push_to_tg or "两步验证" in name or "失败" in name):
            return None
        
        self.n += 1
        filename = f"{self.n:02d}_{self.username[:8]}_{name}.png"
        filepath = os.path.join(SCRIPT_DIR, filename)
        
        try:
            self.driver.save_screenshot(filepath)
            self.shots.append(filepath)
            if push_to_tg:
                self.tg.photo(filepath, caption or name)
            return filepath
        except Exception as e:
            logger.warning(f"截图失败: {e}")
        return None

    def load_cookies(self):
        """加载本地 Cookie"""
        if not os.path.exists(self.cookie_file):
            self.log("未检测到本地 Cookies，将进行登录", "INFO")
            return None
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
                if cookies:
                    self.log("检测到本地 Cookies，尝试复用", "INFO")
                    return cookies
        except Exception as e:
            logger.warning(f"加载 Cookie 失败: {e}")
        return None

    def save_cookies(self, cookies):
        """保存 Cookie"""
        if not cookies:
            return
        try:
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2, ensure_ascii=False)
            self.log("已保存最新 Cookies", "SUCCESS")
        except Exception as e:
            logger.warning(f"保存 Cookie 失败: {e}")

    def is_logged_in(self):
        """检测是否已登录"""
        self.log("正在检测是否已登录到仪表盘...", "INFO")
        
        for attempt in range(3):
            try:
                if "/signin" in self.driver.current_url:
                    return False
                
                try:
                    github_btns = self.driver.find_elements(By.XPATH, "//button[contains(text(), 'GitHub')] | //a[contains(text(), 'GitHub')]")
                    if github_btns:
                        return False
                except:
                    pass
                
                selectors = [
                    (By.XPATH, "//*[contains(text(), 'App Launchpad')]"),
                    (By.XPATH, "//*[contains(text(), 'Database')]"),
                    (By.XPATH, "//*[contains(text(), 'Devbox')]"),
                    (By.XPATH, "//*[contains(text(), 'Object Storage')]"),
                    (By.XPATH, "//*[contains(text(), 'Terminal')]"),
                    (By.CSS_SELECTOR, "input[placeholder*='Search']"),
                    (By.XPATH, "//*[contains(text(), 'Germany')]"),
                    (By.XPATH, "//*[contains(text(), 'Japan')]"),
                ]
                
                for by, selector in selectors:
                    try:
                        elem = WebDriverWait(self.driver, 10).until(
                            EC.visibility_of_element_located((by, selector))
                        )
                        if elem:
                            self.log(f"第 {attempt+1} 次检测成功: 找到元素 {selector}", "SUCCESS")
                            return True
                    except:
                        continue
                        
            except Exception as e:
                logger.debug(f"检测异常: {e}")
            
            self.log(f"第 {attempt+1} 次检测未通过，等待重试...", "WARN")
            time.sleep(5)
            
            try:
                self.driver.refresh()
                time.sleep(3)
            except:
                pass
        
        return False

    def full_github_login(self):
        """执行完整 GitHub 登录流程"""
        self.log("执行完整 GitHub 登录流程", "STEP")
        
        try:
            login_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'GitHub')] | //a[contains(text(), 'GitHub')]"))
            )
        except:
            self.log("未找到登录按钮，说明已登录", "SUCCESS")
            return
        
        self.shot("ClawCloud登录界面")
        login_btn.click()
        self.log("已点击 GitHub 登录按钮", "SUCCESS")
        time.sleep(3)
        
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: "oauth/authorize" in d.current_url
            )
            self.log("检测到 GitHub 授权页面", "SUCCESS")
            self.shot("GitHub授权页")
            
            try:
                auth_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Authorize')]"))
                )
                auth_btn.click()
                self.log("✅ 已自动授权 ClawCloud", "SUCCESS")
                
                try:
                    WebDriverWait(self.driver, 30).until(
                        lambda d: CLAW_CLOUD_URL in d.current_url
                    )
                except:
                    self.driver.get(CLAW_CLOUD_URL)
                
                time.sleep(10)
                self.driver.refresh()
                time.sleep(5)
                self.log("授权完成，已强制刷新仪表盘", "SUCCESS")
                return
                
            except Exception as e:
                logger.debug(f"授权异常: {e}")
                
        except TimeoutException:
            self.log("未检测到授权页，可能需要密码登录", "INFO")
        
        time.sleep(3)
        if "github.com/login" in self.driver.current_url:
            self.log("进入 GitHub 密码登录页面", "INFO")
            
            try:
                username_field = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "login_field"))
                )
                username_field.clear()
                username_field.send_keys(self.username)
                
                password_field = self.driver.find_element(By.ID, "password")
                password_field.clear()
                password_field.send_keys(self.password)
                
                submit_btn = self.driver.find_element(By.CSS_SELECTOR, "input[type='submit']")
                submit_btn.click()
                
                self.log("✅ 已提交账号密码", "SUCCESS")
                self.shot("提交密码后")
                time.sleep(5)
                
                # 检查 2FA
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Two-factor authentication')]"))
                    )
                    self.log("⚠️ 检测到两步验证", "WARN")
                    
                    page_text = self.driver.page_source
                    
                    if "Enter the code from your two-factor authentication app" in page_text:
                        self.authenticator_2fa = True
                        
                        if not self.totp_secret:
                            caption = (
                                f"⚠️ 【第{self.account_index}个账号】检测到 GitHub 两步验证\n\n"
                                "未配置 totp_secret,无法自动填写验证码\n"
                                "请手动输入验证码或配置 2FA 密钥"
                            )
                            self.shot("两步验证页面", push_to_tg=True, caption=caption)
                            self.log("未配置 2FA 密钥,等待60秒手动输入", "WARN")
                            time.sleep(60)
                        else:
                            try:
                                token = pyotp.TOTP(self.totp_secret).now()
                                self.log(f"生成 2FA 验证码: {token}", "INFO")
                                
                                otp_input = None
                                selectors = [
                                    "input#otp",
                                    "input[name='otp']",
                                    "input[placeholder='XXXXXX']",
                                    "input[autocomplete='one-time-code']",
                                    "input[type='tel']",
                                ]
                                
                                for sel in selectors:
                                    try:
                                        otp_input = WebDriverWait(self.driver, 5).until(
                                            EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                                        )
                                        if otp_input:
                                            break
                                    except:
                                        continue
                                
                                if not otp_input:
                                    raise Exception("未找到 OTP 输入框")
                                
                                otp_input.clear()
                                time.sleep(0.5)
                                
                                for char in token:
                                    otp_input.send_keys(char)
                                    time.sleep(0.1)
                                
                                self.log("已输入 2FA 验证码", "INFO")
                                time.sleep(1)
                                
                                try:
                                    submit_selectors = [
                                        "button[type='submit']",
                                        "input[type='submit']",
                                        "button.btn-primary"
                                    ]
                                    
                                    submitted = False
                                    for selector in submit_selectors:
                                        try:
                                            submit_btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                                            submit_btn.click()
                                            self.log(f"已点击提交按钮: {selector}", "INFO")
                                            submitted = True
                                            break
                                        except:
                                            continue
                                    
                                    if not submitted:
                                        otp_input = self.driver.find_element(By.CSS_SELECTOR, selectors[0])
                                        otp_input.send_keys(Keys.RETURN)
                                        self.log("已按回车提交", "INFO")
                                        
                                except Exception as e:
                                    logger.warning(f"提交方式失败: {e}")
                                    try:
                                        otp_input = self.driver.find_element(By.CSS_SELECTOR, selectors[0])
                                        self.driver.execute_script("arguments[0].form.submit();", otp_input)
                                        self.log("已通过 JS 提交表单", "INFO")
                                    except:
                                        pass
                                
                                time.sleep(5)
                                self.log("✅ 2FA 验证码已自动填写并提交", "SUCCESS")
                                
                            except Exception as e:
                                self.log(f"2FA 自动填写失败: {e}", "ERROR")
                                self.shot("2FA失败页面", push_to_tg=True, caption=f"❌ 2FA 自动填写失败: {e}")
                                time.sleep(30)
                    else:
                        self.github_mobile_2fa = True
                        caption = (
                            f"⚠️ 【第{self.account_index}个账号】检测到 GitHub 两步验证（GitHub Mobile）\n\n"
                            "请打开手机 GitHub App，批准登录请求\n"
                            "脚本已等待60秒供您操作，完成后会自动继续"
                        )
                        self.shot("两步验证页面", push_to_tg=True, caption=caption)
                        self.log("等待60秒让你手动批准 GitHub Mobile 2FA...", "WARN")
                        
                        try:
                            WebDriverWait(self.driver, 60).until(
                                lambda d: "oauth/authorize" in d.current_url or CLAW_CLOUD_URL in d.current_url
                            )
                            self.log("2FA 批准成功，继续流程", "SUCCESS")
                        except TimeoutException:
                            self.log("2FA 等待超时，尝试强制继续", "WARN")
                            
                except TimeoutException:
                    self.log("未检测到 2FA，继续流程", "INFO")
                
                time.sleep(5)
                if "oauth/authorize" in self.driver.current_url:
                    try:
                        auth_btn = WebDriverWait(self.driver, 10).until(
                            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Authorize')]"))
                        )
                        auth_btn.click()
                        self.log("✅ 密码后自动授权", "SUCCESS")
                        time.sleep(5)
                    except:
                        pass
                
            except Exception as e:
                self.log(f"密码登录异常: {e}", "ERROR")
                self.shot("登录异常页面")
        
        try:
            WebDriverWait(self.driver, 30).until(
                lambda d: CLAW_CLOUD_URL in d.current_url
            )
            self.log("已跳转回 ClawCloud", "SUCCESS")
        except:
            self.log("未自动返回，强制跳转首页", "WARN")
            self.driver.get(CLAW_CLOUD_URL)
        
        time.sleep(10)
        self.driver.refresh()
        time.sleep(10)
        self.log("已强制刷新，确保仪表盘完全加载", "SUCCESS")

    def keepalive(self):
        """保活访问"""
        self.log("开始保活访问...", "STEP")
        
        urls = [
            (f"{CLAW_CLOUD_URL}/", "首页"),
            (f"{CLAW_CLOUD_URL}/apps", "Apps页面")
        ]
        
        for url, name in urls:
            try:
                self.driver.get(url)
                time.sleep(5)
                self.log(f"保活访问: {name}", "SUCCESS")
            except Exception as e:
                self.log(f"访问失败: {e}", "WARN")

    def generate_notify_content(self):
        """生成通知内容"""
        if self.used_old_cookie:
            login_way = "使用Cookies授权登录"
        elif self.authenticator_2fa:
            login_way = "Authenticator app自动登录"
        elif self.github_mobile_2fa:
            login_way = "GitHub Mobile手动批准登录"
        else:
            login_way = "使用Cookies授权登录"

        display_user = self.username[:3] + "**" if "@" not in self.username[:3] else self.username.split("@")[0][:3] + "**"
        balance_display = self.balance if self.balance.startswith('$') else f"${self.balance}"

        important_lines = []
        priority_keywords = [
            "已强制刷新，确保仪表盘完全加载",
            r"第 \d+ 次检测成功: 找到元素",
            "已保存最新 Cookies"
        ]
        
        for keyword in priority_keywords:
            pattern = re.compile(keyword)
            for log in self.logs:
                if pattern.search(log):
                    important_lines.append(log)
                    break

        result_text = "✅ 成功" if self.success else "❌ 失败"

        content = f"登录逻辑： {login_way}\n"
        content += f"用户： {display_user}\n"
        content += "重要信息：\n"
        for line in important_lines[:3]:
            if line:
                content += f"✅ [第{self.account_index}个账号] {line}\n"
        content += f"💵当前剩余：{balance_display}\n"
        content += f"保活结果： {result_text}\n"
        content += f"时间： {time.strftime('%Y-%m-%d %H:%M:%S')}"

        self.notify_content = content

    def cleanup_screenshots(self):
        """清理截图"""
        deleted = 0
        for p in self.shots:
            try:
                if os.path.exists(p):
                    os.remove(p)
                    deleted += 1
            except:
                pass
        if deleted > 0:
            self.log(f"已清理 {deleted} 张截图", "SUCCESS")

    def find_chrome(self):
        """查找 Chrome/Chromium - Ubuntu 版本"""
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]
        for path in candidates:
            if os.path.exists(path):
                logger.info(f"找到 Chrome: {path}")
                return path
        return None

    def find_chromedriver(self):
        """查找 ChromeDriver - Ubuntu 版本"""
        candidates = [
            "/usr/bin/chromedriver",
            "/usr/local/bin/chromedriver",
            "/snap/bin/chromedriver",
        ]
        for path in candidates:
            if os.path.exists(path):
                logger.info(f"找到 ChromeDriver: {path}")
                return path
        return None

    def run(self):
        """运行保活流程"""
        self.log("开始运行保活流程", "STEP")
        
        chrome_path = self.find_chrome()
        if not chrome_path:
            self.log("未找到 Chrome/Chromium", "ERROR")
            self.log("请安装: sudo apt install chromium-browser chromium-chromedriver", "ERROR")
            self.success = False
            self.generate_notify_content()
            return self.notify_content
        
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.binary_location = chrome_path
        
        try:
            chromedriver_path = self.find_chromedriver()
            if chromedriver_path:
                service = Service(executable_path=chromedriver_path)
                self.driver = webdriver.Chrome(service=service, options=options)
            else:
                self.driver = webdriver.Chrome(options=options)
            
            self.log("浏览器启动成功", "SUCCESS")
            
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            
            if self.old_cookies:
                self.driver.get(CLAW_CLOUD_URL)
                time.sleep(2)
                
                for cookie in self.old_cookies:
                    try:
                        self.driver.add_cookie(cookie)
                    except:
                        pass
                
                self.log("已注入本地 Cookies", "SUCCESS")
            
            self.driver.get(CLAW_CLOUD_URL)
            time.sleep(10)
            self.shot("打开主页后")
            
            if self.is_logged_in():
                self.log("🎉 已登录，直接保活", "SUCCESS")
                self.used_old_cookie = True
            else:
                self.log("检测到未登录，执行登录流程", "WARN")
                self.full_github_login()
                
                if self.is_logged_in():
                    self.log("✅ 登录/授权最终成功！", "SUCCESS")
                else:
                    self.log("❌ 登录最终失败", "ERROR")
                    self.success = False
                    self.shot("最终失败页面", push_to_tg=True, caption="❌ 保活失败，请手动检查")
                    self.generate_notify_content()
                    return self.notify_content
            
            try:
                balance_elem = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(), '$')]"))
                )
                raw_balance = balance_elem.text.strip()
                match = re.search(r'\$[\d.,]+', raw_balance)
                if match:
                    self.balance = match.group()
                    self.log(f"成功提取余额: {self.balance}", "SUCCESS")
                else:
                    self.balance = raw_balance
            except:
                self.balance = "提取失败"
                self.log("未能提取到余额", "WARN")
            
            current_cookies = self.driver.get_cookies()
            if current_cookies:
                filtered_cookies = [
                    c for c in current_cookies 
                    if 'github.com' in c.get('domain', '') or 'claw.cloud' in c.get('domain', '')
                ]
                if filtered_cookies:
                    self.save_cookies(filtered_cookies)
            
            self.keepalive()
            self.generate_notify_content()
            
        except Exception as e:
            self.log(f"运行异常: {e}", "ERROR")
            logger.exception(e)
            self.success = False
            self.generate_notify_content()
            
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            
            self.cleanup_screenshots()
        
        return self.notify_content


if __name__ == "__main__":
    print("\n" + "="*60)
    print("💻 抓云多账号自动保活 - Ubuntu ARM64 版本")
    print("="*60 + "\n")

    if not ACCOUNTS:
        print("❌ 错误: 未配置任何账号!")
        print("\n请配置环境变量 CLAW_ACCOUNTS 或修改脚本中的 ACCOUNTS_CONFIG")
        print("格式: 账号1@example.com----密码1----2FA密钥1&账号2@example.com----密码2----2FA密钥2\n")
        sys.exit(1)
    
    print(f"📊 共配置 {len(ACCOUNTS)} 个账号\n")

    all_notify_contents = []
    has_screenshot_triggered = False

    for idx, acc in enumerate(ACCOUNTS, 1):
        print(f"正在处理第 {idx} 个账号: {acc['username']}")
        instance = AutoLogin(acc, idx)
        content = instance.run()
        
        if content:
            all_notify_contents.append(f"【账号{idx}保活信息】\n{content}")

        if instance.shots:
            has_screenshot_triggered = True

        print(f"第 {idx} 个账号处理完成\n")
        time.sleep(10)

    if all_notify_contents:
        final_msg = f"<b>💻 抓云自动保活 - Ubuntu ARM64</b>\n\n"
        final_msg += f"🔥一共有{len(ACCOUNTS)}个账号🔥\n\n"
        final_msg += "\n\n==========================\n\n".join(all_notify_contents)
        final_msg += "\n\n==========================\n\n"
        final_msg += "🗑️ 本次运行截图已清理\n" if has_screenshot_triggered else "🗑️ 本次运行脚本没有触发截图\n"
        final_msg += "\n\n==========================\n\n"
        final_msg += f"网页登录地址：{CLAW_CLOUD_URL}\n"
        final_msg += "\n\n==========================\n\n"

        tg = Telegram()
        tg.send(final_msg)
    
    print("\n" + "="*60)
    print("✅ 所有账号处理完成")
    print("="*60 + "\n")
