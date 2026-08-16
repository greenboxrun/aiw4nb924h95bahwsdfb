from selenium import webdriver


URL = "https://www.issuelink.co.kr/community/listview/all/72/comment/_self/blank/blank/blank"


def main() -> None:
    driver = webdriver.Chrome()
    driver.get(URL)

    input("Press Enter to close the browser...")
    driver.quit()


if __name__ == "__main__":
    main()
