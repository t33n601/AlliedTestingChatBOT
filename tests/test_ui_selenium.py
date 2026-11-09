"""E2E test: opens the Gradio chat in a real Chrome served by Selenoid.
Needs: Selenoid on :4445 (selenoid/start-selenoid.sh) and chat_ui.py running."""
import os

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SELENOID_URL = os.getenv("KB_SELENOID_URL", "http://localhost:4445/wd/hub")
# the browser runs inside Docker, so "localhost" there is the container itself
APP_URL = os.getenv("KB_APP_URL", "http://host.docker.internal:7871")


@pytest.fixture
def browser():
    options = webdriver.ChromeOptions()
    options.set_capability("selenoid:options", {
        "enableVNC": True,
        "enableVideo": True,
        "videoName": "kb_ui_test.mp4",
    })
    driver = webdriver.Remote(command_executor=SELENOID_URL, options=options)
    driver.set_window_size(1280, 900)
    yield driver
    driver.quit()


def test_open_chat_ui(browser):
    browser.get(APP_URL)
    wait = WebDriverWait(browser, 30)
    textbox = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "textarea")))
    assert "KB Helper" in browser.page_source
    assert textbox.is_displayed()


def test_ask_question(browser):
    browser.get(APP_URL)
    wait = WebDriverWait(browser, 300)
    textbox = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea")))
    textbox.send_keys("What services do you offer?")
    textbox.send_keys("\n")
    # the bot's reply appears as a second message bubble
    wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".message-row")) >= 2
               and "Error" not in d.find_elements(By.CSS_SELECTOR, ".message-row")[-1].text
               and d.find_elements(By.CSS_SELECTOR, ".message-row")[-1].text.strip())
