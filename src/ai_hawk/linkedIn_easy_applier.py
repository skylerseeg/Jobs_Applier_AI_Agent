import base64
import json
import os
import random
import re
import time
import traceback
from typing import List, Optional, Any, Tuple

from httpx import HTTPStatusError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from selenium.common.exceptions import NoSuchElementException, TimeoutException, JavascriptException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support import ui

from job_application import JobApplication
from job_application_saver import ApplicationSaver
import src.utils as utils
from src.logging import logger
from src.job import Job
from src.ai_hawk.llm.llm_manager import GPTAnswerer
from utils import browser_utils
import utils.time_utils

# TODO: move this to its own class like QuestionManager or something
def question_already_exists_in_data(question: str, data: List[dict]) -> bool:
    """
    Check if a question already exists in the data list.

    Args:
        question: The question text to search for
        data: List of question dictionaries to search through

    Returns:
        bool: True if question exists, False otherwise
    """
    return any(item['question'] == question for item in data)

# TODO: refactor this class for better separation of concern and improved maintainability (God Class anti-pattern)
# Note: "Easy Applier" is not a single purpose and must be broken down further. For example, this can become a
#   type of form controller. A parent class (FormController) could have all the selenium form controls implemented,
#   while derived classes inherit those methods and implement their own form-specific controls (find_easy_apply_button)
#   Further, you don't need to make everything a class member. If you don't need to initialize it in __init__, it
#   shouldn't be member of the class.
class AIHawkEasyApplier:
    def __init__(self, driver: Any, resume_dir: Optional[str], set_old_answers: List[Tuple[str, str, str]],
                 gpt_answerer: GPTAnswerer, resume_generator_manager):
        logger.debug("Initializing AIHawkEasyApplier")
        self.driver = driver
        self.resume_path = resume_dir
        self.set_old_answers = set_old_answers
        self.gpt_answerer = gpt_answerer
        self.resume_generator_manager = resume_generator_manager
        self.all_data = self._load_questions_from_json()
        self.current_job = None

        if resume_dir is None or not os.path.exists(resume_dir):
            resume_dir = None

        logger.debug("AIHawkEasyApplier initialized successfully")

    # TODO: move this in the same class as question_already_exists_in_data()
    def _load_questions_from_json(self) -> List[dict]:
        """
        Helper function to load questions from a JSON file.
        
        This helper function is called by the constructor, so it should not 
        throw exceptions. Rather, it should handle them gracefully.

        Returns:
            List[dict]: The list of questions loaded from the JSON file or an empty
                list if there are errors.
        """
        output_file = 'answers.json'
        logger.debug(f"Loading questions from JSON file: {output_file}")

        try:
            with open(output_file, "r", encoding="utf-8") as f:
                file_content = f.read()
        except FileNotFoundError:
            logger.error("JSON file not found, returning empty list.")
            return []

        try:
            data = json.loads(file_content)

            if not isinstance(data, list):
                return []

            logger.debug("Questions loaded successfully from JSON")
            return data
        except json.JSONDecodeError as e:
            logger.error(f"JSON decoding failed: {e}")
            return []
        except TypeError as e:
            logger.error(f"Type error while parsing JSON: {e}")
            return []

    # TODO: refactor this to return something meaningful (if anything) instead of throwing an exception
    def check_for_premium_redirect(self, job: Any, max_attempts=3) -> None:
        """
        Attempts to return to job after being redirected to premium page

        Raises:
            Exception (for now)
        """
        current_url = self.driver.current_url
        attempts = 0

        while "linkedin.com/premium" in current_url and attempts < max_attempts:
            logger.warning("Redirected to linkedIn Premium page. Attempting to return to job page.")
            attempts += 1
            self.driver.get(job.link)
            time.sleep(2)
            current_url = self.driver.current_url

        if "linkedin.com/premium" in current_url:
            logger.error(f"Failed to return to job page after {max_attempts} attempts. Cannot apply for the job.")
            raise Exception(
                f"Redirected to linkedIn Premium page and failed to return after {max_attempts} attempts. Job application aborted.")
            
    def apply_to_job(self, job: Job) -> None:
        """
        Starts the process of applying to a job.

        Args:
            job: A job object with the job details.

        Returns: None
        """
        logger.debug(f"Applying to job: {job}")

        try:
            self.job_apply(job)
            logger.info(f"Successfully applied to job: {job.title}")
        except Exception:
            logger.exception(f"Failed to apply to job: {job.title}")
            raise

    def _handle_general_exception(self, job: Job) -> None:
        """Helper function for repeated redirect checks"""
        logger.exception(f"Failed to apply to job: {job}")
        logger.debug("Discarding application due to failure")
        self._discard_application()

    def job_apply(self, job: Job):
        logger.debug(f"Starting job application for job: {job}")
        self.job_application = JobApplication(job)

        try:
            self.driver.get(job.link)
            logger.debug(f"Navigated to job link: {job.link}")
        except Exception as e:
            logger.error(f"Failed to navigate to job link: {job.link}, error: {str(e)}")
            raise

        utils.time_utils.medium_sleep()
        self.check_for_premium_redirect(job)

        try:
            self.driver.execute_script("document.activeElement.blur();")
        except JavascriptException:
            logger.exception("Failed to execute blur() script")
            raise

        logger.debug("Focus removed from the active element")

        try:
            self.check_for_premium_redirect(job)
        except Exception:
            self._handle_general_exception(job)
            raise

        try:
            easy_apply_button = self._find_easy_apply_button(job)
        except TimeoutException as exc:
            logger.exception("Couldn't find 'easy apply' button (how many times have you seen this log message?)")
            raise Exception from exc

        try:
            self.check_for_premium_redirect(job)
        except Exception:
            self._handle_general_exception(job)
            raise

        logger.debug("Retrieving job description")

        try:
            job_description = self._get_job_description()
        except NoSuchElementException as exc:
            logger.exception("No job description")
            raise Exception from exc
        except Exception:
            logger.exception("Caught general exception for some reason")
            raise

        job.set_job_description(job_description)
        logger.debug(f"Job description set: {job_description[:100]}")
        logger.debug("Retrieving recruiter link")

        try:
            recruiter_link = self._get_job_recruiter()
        except TimeoutException as exc:
            logger.exception("Driver timed out")
            raise Exception from exc
        except Exception:
            logger.exception(f"Failed to apply to job: {job}")
            logger.debug("Discarding application due to failure")
            self._discard_application()
            raise

        job.set_recruiter_link(recruiter_link)
        logger.debug(f"Recruiter link set: {recruiter_link}")
        self.current_job = job
        logger.debug("Passing job information to GPT Answerer")
        self.gpt_answerer.set_job(job)

        if not self.gpt_answerer.is_job_suitable():
            return

        logger.debug("Attempting to click 'Easy Apply' button")
        actions = ActionChains(self.driver)
        actions.move_to_element(easy_apply_button).click().perform()
        logger.debug("'Easy Apply' button clicked successfully")
        logger.debug("Filling out application form")
        self._fill_application_form(job)
        logger.debug(f"Job application process completed successfully for job: {job}")

    # FIXME: implement proper flow control and remove excessive exception raising
    def _find_easy_apply_button(self, job: Any, max_attempts = 2) -> WebElement:
        logger.debug("Searching for 'Easy Apply' button")
        attempt = 0

        search_methods = [
            {
                'description': "find all 'Easy Apply' buttons using find_elements",
                'find_elements': True,
                'xpath': '//button[contains(@class, "jobs-apply-button") and contains(., "Easy Apply")]'
            },
            {
                'description': "'aria-label' containing 'Easy Apply to'",
                'xpath': '//button[contains(@aria-label, "Easy Apply to")]'
            },
            {
                'description': "button text search",
                'xpath': '//button[contains(text(), "Easy Apply") or contains(text(), "Apply now")]'
            }
        ]

        while attempt < max_attempts:
            try:
                self.check_for_premium_redirect(job)
            except Exception:
                # TODO: refactor to return None or implement logic for what should happen if unable to return from redirect
                logger.exception("Redirected")
                raise

            self._scroll_page()

            for method in search_methods:
                logger.debug(f"Attempting search using {method['description']}")

                buttons = self.driver.find_elements(By.XPATH, method['xpath'])

                if not buttons:
                    logger.debug(f"No buttons found using {method['description']}")
                    continue

                for index, button in enumerate(buttons):
                    try:
                        ui.WebDriverWait(self.driver, 10).until(EC.visibility_of(button))
                    except TimeoutException:
                        logger.debug(f"'Easy Apply' button {index + 1} not visible")
                        continue

                    logger.debug(f"Found 'Easy Apply' button {index + 1}, attempting to click")

                    try:
                        ui.WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable(button))
                        return button
                    except TimeoutException as e:
                        logger.warning(f"Button {index + 1} found but not clickable: {e}")
                        continue

            self.check_for_premium_redirect(job)

            if attempt == 0:
                logger.debug("Refreshing page to retry finding 'Easy Apply' button")
                self.driver.refresh()
                time.sleep(random.randint(3, 5))
            attempt += 1

        page_url = self.driver.current_url
        logger.error(f"No clickable 'Easy Apply' button found after {max_attempts + 1} attempts. page url: {page_url}")
        raise Exception("No clickable 'Easy Apply' button found")

    def _get_job_description(self) -> str:
        logger.debug("Getting job description")

        # TODO: un-nest try-blocks
        try:
            try:
                see_more_button = self.driver.find_element(
                    By.XPATH, '//button[@aria-label="Click to see more description"]')
                actions = ActionChains(self.driver)
                actions.move_to_element(see_more_button).click().perform()
                time.sleep(2)
            except NoSuchElementException:
                logger.debug("See more button not found, skipping")

            try:
                description = self.driver.find_element(
                    By.CLASS_NAME, 'jobs-description-content__text').text
            except NoSuchElementException:
                logger.debug(
                    "First class not found, checking for second class for premium members")
                description = self.driver.find_element(
                    By.CLASS_NAME, 'job-details-about-the-job-module__description').text

            logger.debug("Job description retrieved successfully")
            return description
        except NoSuchElementException:
            logger.exception("Job description not found")
            raise
        except Exception:
            logger.exception(f"Error getting Job description")
            raise

    def _get_job_recruiter(self) -> str:
        """
        Retreive job poster's profile link
        
        Returns:
            str: Recruiter link
        
        Raises:
            TimeoutException: If the WebDriver times out while waiting
        """
        logger.debug("Getting job recruiter information")

        try:
            hiring_team_section = ui.WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//h2[text()="Meet the hiring team"]')
                )
            )
        except TimeoutException:
            logger.exception("Timed out waiting for WebDriver")
            raise

        logger.debug("Hiring team section found")
        recruiter_elements = hiring_team_section.find_elements(By.XPATH, './/following::a[contains(@href, "linkedin.com/in/")]')

        if recruiter_elements:
            recruiter_element = recruiter_elements[0]
            recruiter_link = recruiter_element.get_attribute('href')
            logger.debug(f"Job recruiter link retrieved successfully: {recruiter_link}")
            return recruiter_link

        logger.debug("No recruiter link found in the hiring team section")
        return ""

    def _scroll_page(self) -> None:
        logger.debug("Scrolling the page")
        scrollable_element = self.driver.find_element(By.TAG_NAME, 'html')
        browser_utils.scroll_slow(self.driver, scrollable_element, step=300, reverse=False)
        browser_utils.scroll_slow(self.driver, scrollable_element, step=300, reverse=True)

    def _fill_application_form(self, job):
        logger.debug(f"Filling out application form for job: {job}")

        while True:
            self.fill_up(job)

            if self._next_or_submit():
                ApplicationSaver.save(self.job_application)
                logger.debug("Application form saved")
                break

    def _next_or_submit(self):
        logger.debug("Clicking 'Next' or 'Submit' button")
        next_button = self.driver.find_element(By.CLASS_NAME, "artdeco-button--primary")
        button_text = next_button.text.lower()

        if 'submit application' in button_text:
            logger.debug("Submit button found, submitting application")
            self._unfollow_company()
            utils.time_utils.short_sleep()
            next_button.click()
            utils.time_utils.short_sleep()
            return True
        utils.time_utils.short_sleep()
        next_button.click()
        utils.time_utils.medium_sleep()
        self._check_for_errors()

    def _unfollow_company(self) -> None:
        logger.debug("Unfollowing company")

        try:
            follow_checkbox = self.driver.find_element(
                By.XPATH, "//label[contains(.,'to stay up to date with their page.')]")
        except NoSuchElementException as e:
            logger.debug(f"Failed to unfollow company: {e}")

        # TODO: verify checkbox is checked first
        follow_checkbox.click()

    # TODO: refactor this to return something meaningful instead of throwing an exception
    def _check_for_errors(self) -> None:
        logger.debug("Checking for form errors")
        error_elements = self.driver.find_elements(
            By.CLASS_NAME, 'artdeco-inline-feedback--error')

        error_elements = self.driver.find_elements(
            By.CLASS_NAME, 'artdeco-inline-feedback--error')

        if error_elements:
            logger.error(f"Form submission failed with errors: {error_elements}")
            raise Exception(f"Failed answering or file upload. {str([e.text for e in error_elements])}")

    def _discard_application(self) -> None:
        logger.debug("Discarding application")

        try:
            self.driver.find_element(By.CLASS_NAME, 'artdeco-modal__dismiss').click()
        except NoSuchElementException as e:
            logger.warning(f"Failed to discard application: {e}")
            return

        utils.time_utils.medium_sleep()
        confirm_button = self.driver.find_elements(
            By.CLASS_NAME, 'artdeco-modal__confirm-dialog-btn')

        if not confirm_button:
            logger.warning(
                "Attempted to confirm discard. User navigated away.")
            return

        confirm_button[0].click()
        utils.time_utils.medium_sleep()

    def _save_job_application_process(self) -> None:
        logger.debug("Application not completed. Saving job to My Jobs, In Progess section")
        
        try:
            self.driver.find_element(By.CLASS_NAME, 'artdeco-modal__dismiss').click()
            utils.time_utils.medium_sleep()
            self.driver.find_elements(By.CLASS_NAME, 'artdeco-modal__confirm-dialog-btn')[1].click()
            utils.time_utils.medium_sleep()
        except Exception as e:
            logger.error(f"Failed to save application process: {e}")

    # TODO: fix lazy exception handling
    def fill_up(self, job) -> None:
        logger.debug(f"Filling up form sections for job: {job}")

        try:
            easy_apply_content = ui.WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.CLASS_NAME, 'jobs-easy-apply-content')
                    )
                )
            easy_apply_content = ui.WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.CLASS_NAME, 'jobs-easy-apply-content'))
            )

            pb4_elements = easy_apply_content.find_elements(
                By.CLASS_NAME, 'pb4')
            for element in pb4_elements:
                self._process_form_element(element, job)
        except Exception as e:
            logger.error(f"Failed to find form elements: {e}")

    def _process_form_element(self, element: WebElement, job) -> None:
        logger.debug("Processing form element")

        if self._is_upload_field(element):
            self._handle_upload_fields(element, job)
        else:
            self._fill_additional_questions()

    def _handle_dropdown_fields(self, element: WebElement) -> None:
        logger.debug("Handling dropdown fields")

        dropdown = element.find_element(By.TAG_NAME, 'select')
        select = ui.Select(dropdown)
        dropdown_id = dropdown.get_attribute('id')

        if 'phoneNumber-Country' in dropdown_id:
            country = self.resume_generator_manager.get_resume_country()

            if country:
                try:
                    select.select_by_value(country)
                    logger.debug(f"Selected phone country: {country}")
                    return
                except NoSuchElementException:
                    logger.warning(f"Country {country} not found in dropdown options")

        options = [option.text for option in select.options]
        logger.debug(f"Dropdown options found: {options}")

        parent_element = dropdown.find_element(By.XPATH, '../..')

        label_elements = parent_element.find_elements(By.TAG_NAME, 'label')

        if label_elements:
            question_text = label_elements[0].text.lower()
        else:
            question_text = "unknown"

        logger.debug(f"Detected question text: {question_text}")

        existing_answer = None
        current_question_sanitized = self._sanitize_text(question_text) 

        for item in self.all_data:
            if current_question_sanitized in item['question'] and item['type'] == 'dropdown':
                existing_answer = item['answer']
                break

        if existing_answer:
            logger.debug(f"Found existing answer for question '{question_text}': {existing_answer}")
        else:
            logger.debug(f"No existing answer found, querying model for: {question_text}")
            existing_answer = self.gpt_answerer.answer_question_from_options(question_text, options)
            logger.debug(f"No existing answer found, querying model for: {question_text}")
            existing_answer = self.gpt_answerer.answer_question_from_options(question_text, options)
            logger.debug(f"Model provided answer: {existing_answer}")
            self._save_questions_to_json({'type': 'dropdown', 'question': question_text, 'answer': existing_answer})
            self.all_data = self._load_questions_from_json()

        if existing_answer in options:
            select.select_by_visible_text(existing_answer)
            logger.debug(f"Selected option: {existing_answer}")
            self.job_application.save_application_data({'type': 'dropdown', 'question': question_text, 'answer': existing_answer})
        else:
            logger.error(f"Answer '{existing_answer}' is not a valid option in the dropdown")
            raise Exception(f"Invalid option selected: {existing_answer}")

    def _is_upload_field(self, element: WebElement) -> bool:
        is_upload = bool(element.find_elements(By.XPATH, ".//input[@type='file']"))
        logger.debug(f"Element is upload field: {is_upload}")
        return is_upload

    def _handle_upload_fields(self, element: WebElement, job) -> None:
        logger.debug("Handling upload fields")

        try:
            show_more_button = self.driver.find_element(By.XPATH,
                                                        "//button[contains(@aria-label, 'Show more resumes')]")
            show_more_button.click()
            logger.debug("Clicked 'Show more resumes' button")
        except NoSuchElementException:
            logger.debug("'Show more resumes' button not found, continuing...")

        file_upload_elements = self.driver.find_elements(By.XPATH, "//input[@type='file']")
        
        for element in file_upload_elements:
            parent = element.find_element(By.XPATH, "..")
            self.driver.execute_script("arguments[0].classList.remove('hidden')", element)

            output = self.gpt_answerer.resume_or_cover(parent.text.lower())

            if 'resume' in output:
                logger.debug("Uploading resume")

                if self.resume_path is not None and self.resume_path.resolve().is_file():
                    element.send_keys(str(self.resume_path.resolve()))
                    logger.debug(f"Resume uploaded from path: {self.resume_path.resolve()}")
                else:
                    logger.debug("Resume path not found or invalid, generating new resume")
                    self._create_and_upload_resume(element, job)
            elif 'cover' in output:
                logger.debug("Uploading cover letter")
                self._create_and_upload_cover_letter(element, job)

        logger.debug("Finished handling upload fields")

    # TODO: before handling HTTPStatusError, create a unit test to verify it gets thrown
    def _create_and_upload_resume(self, element, job : Job):
        logger.debug("Starting the process of creating and uploading resume.")
        folder_path = 'generated_cv'

        if not os.path.exists(folder_path):
            logger.debug(f"Creating directory at path: {folder_path}")

            try:
                os.makedirs(folder_path, exist_ok=True)
            except OSError:
                logger.error(f"Failed to create directory: {folder_path}.")
                raise

        while True:
            timestamp = int(time.time())

            try:
                file_path_pdf = os.path.join(folder_path, f"CV_{timestamp}.pdf")
            except (TypeError, AttributeError, BytesWarning):
                logger.error("Failed to create CV path")

            logger.debug(f"Generated file path for resume: {file_path_pdf}")
            logger.debug(f"Generating resume for job: {job.title} at {job.company}")

            try:
                resume_pdf_base64 = self.resume_generator_manager.pdf_base64(job_description_text=job.description)
            except ValueError:
                logger.error("Failed to generate PDF")
                raise

            logger.debug(f"Generating resume for job: {job.title} at {job.company}")
            
            try:
                with open(file_path_pdf, "xb") as f:
                    f.write(base64.b64decode(resume_pdf_base64))
            except FileNotFoundError:
                logger.error("New CV not found")
                raise

            logger.debug(f"Resume successfully generated and saved to: {file_path_pdf}")

            break

        file_size = os.path.getsize(file_path_pdf)
        max_file_size = 2 * 1024 * 1024  # 2 MB
        logger.debug(f"Resume file size: {file_size} bytes")

        if file_size > max_file_size:
            logger.error(f"Resume file size exceeds 2 MB: {file_size} bytes")
            raise ValueError("Resume file size exceeds the maximum limit of 2 MB.")

        allowed_extensions = {'.pdf', '.doc', '.docx'}
        file_extension = os.path.splitext(file_path_pdf)[1].lower()
        logger.debug(f"Resume file extension: {file_extension}")

        if file_extension not in allowed_extensions:
            logger.error(f"Invalid resume file format: {file_extension}")
            raise ValueError("Resume file format is not allowed. Only PDF, DOC, and DOCX formats are supported.")

        try:
            logger.debug(f"Uploading resume from path: {file_path_pdf}")
            element.send_keys(os.path.abspath(file_path_pdf))
            job.resume_path = os.path.abspath(file_path_pdf)
            self.job_application.resume_path = os.path.abspath(file_path_pdf)
            time.sleep(2)
            logger.debug(f"Resume created and uploaded successfully: {file_path_pdf}")
        except Exception:
            logger.exception(f"Resume upload failed")
            raise

    # TODO: refactor: this should be broken down further and less specific
    # Examples:
    #   DocumentGenerator.generate_pdf(self, save_path: Path, bot: Answerer, prompt: str)
    #   JobApplicationFormController.upload_pdf(self, path: Path, element: WebElement)
    def _create_and_upload_cover_letter(self, element: WebElement, job : Job) -> None:
        logger.debug("Starting the process of creating and uploading cover letter.")
        cover_letter_text = self.gpt_answerer.answer_question_textual_wide_range("Write a cover letter")
        folder_path = "generated_cover_letters"

        try:
            if not os.path.exists(folder_path):
                logger.debug(f"Creating directory at path: {folder_path}")
            os.makedirs(folder_path, exist_ok=True)
        except OSError:
            logger.error(f"Failed to create directory: {folder_path}.")
            raise

        while True:
            try:
                timestamp = int(time.time())
                file_path_pdf = os.path.join(folder_path, f"Cover_Letter_{timestamp}.pdf")
                logger.debug(f"Generated file path for cover letter: {file_path_pdf}")
                file_path_pdf = os.path.join(folder_path, f"Cover_Letter_{timestamp}.pdf")
                logger.debug(f"Generated file path for cover letter: {file_path_pdf}")

                c = canvas.Canvas(file_path_pdf, pagesize=A4)
                page_width, page_height = A4
                text_object = c.beginText(50, page_height - 50)
                text_object.setFont("Helvetica", 12)

                max_width = page_width - 100
                bottom_margin = 50

                def split_text_by_width(text, font, font_size, max_width):
                    wrapped_lines = []

                    for line in text.splitlines():
                        if stringWidth(line, font, font_size) > max_width:
                            words = line.split()
                            new_line = ""

                            for word in words:
                                if stringWidth(new_line + word + " ", font, font_size) <= max_width:
                                    new_line += word + " "
                                else:
                                    wrapped_lines.append(new_line.strip())
                                    new_line = word + " "

                            wrapped_lines.append(new_line.strip())
                        else:
                            wrapped_lines.append(line)
                    return wrapped_lines

                lines = split_text_by_width(cover_letter_text, "Helvetica", 12, max_width)

                for line in lines:
                    text_height = text_object.getY()

                    if text_height > bottom_margin:
                        text_object.textLine(line)
                    else:
                        c.drawText(text_object)
                        c.showPage()
                        text_object = c.beginText(50, page_height - 50)
                        text_object.setFont("Helvetica", 12)
                        text_object.textLine(line)

                c.drawText(text_object)
                c.save()
                logger.debug(f"Cover letter successfully generated and saved to: {file_path_pdf}")

                break
            except Exception as e:
                logger.error(f"Failed to generate cover letter: {e}")
                tb_str = traceback.format_exc()
                logger.error(f"Traceback: {tb_str}")
                raise

        file_size = os.path.getsize(file_path_pdf)
        max_file_size = 2 * 1024 * 1024  # 2 MB
        logger.debug(f"Cover letter file size: {file_size} bytes")

        if file_size > max_file_size:
            logger.error(f"Cover letter file size exceeds 2 MB: {file_size} bytes")
            raise ValueError("Cover letter file size exceeds the maximum limit of 2 MB.")

        allowed_extensions = {'.pdf', '.doc', '.docx'}
        file_extension = os.path.splitext(file_path_pdf)[1].lower()
        logger.debug(f"Cover letter file extension: {file_extension}")

        if file_extension not in allowed_extensions:
            logger.error(f"Invalid cover letter file format: {file_extension}")
            raise ValueError("Cover letter file format is not allowed. Only PDF, DOC, and DOCX formats are supported.")

        try:
            logger.debug(f"Uploading cover letter from path: {file_path_pdf}")
            element.send_keys(os.path.abspath(file_path_pdf))
            job.cover_letter_path = os.path.abspath(file_path_pdf)
            self.job_application.cover_letter_path = os.path.abspath(file_path_pdf)
            time.sleep(2)
            logger.debug(f"Cover letter created and uploaded successfully: {file_path_pdf}")
        except Exception:
            logger.exception(f"Cover letter upload failed: {tb_str}")
            raise

    def _fill_additional_questions(self) -> None:
        logger.debug("Filling additional questions")
        form_sections = self.driver.find_elements(By.CLASS_NAME, 'jobs-easy-apply-form-section__grouping')

        for section in form_sections:
            self._process_form_section(section)

    def _process_form_section(self, section: WebElement) -> None:
        logger.debug("Processing form section")

        if self._handle_terms_of_service(section):
            logger.debug("Handled terms of service")
            return

        if self._find_and_handle_radio_question(section):
            logger.debug("Handled radio question")
            return

        if self._find_and_handle_textbox_question(section):
            logger.debug("Handled textbox question")
            return

        if self._find_and_handle_date_question(section):
            logger.debug("Handled date question")
            return

        if self._find_and_handle_dropdown_question(section):
            logger.debug("Handled dropdown question")
            return

    def _handle_terms_of_service(self, element: WebElement) -> bool:
        checkbox = element.find_elements(By.TAG_NAME, 'label')

        if checkbox and any(
                term in checkbox[0].text.lower() for term in ['terms of service', 'privacy policy', 'terms of use']):
            checkbox[0].click()
            logger.debug("Clicked terms of service checkbox")
            return True

        return False

    def _find_and_handle_radio_question(self, section: WebElement) -> bool:
        question = section.find_element(By.CLASS_NAME, 'jobs-easy-apply-form-element')
        radios = question.find_elements(By.CLASS_NAME, 'fb-text-selectable__option')

        if radios:
            question_text = section.text.lower()
            options = [radio.text.lower() for radio in radios]
            existing_answer = None
            current_question_sanitized = self._sanitize_text(question_text)

            for item in self.all_data:
                if current_question_sanitized in item['question'] and item['type'] == 'radio':
                    existing_answer = item
                    break

            if existing_answer:
                self._select_radio(radios, existing_answer['answer'])
                self.job_application.save_application_data(existing_answer)
                logger.debug("Selected existing radio answer")
                return True

            answer = self.gpt_answerer.answer_question_from_options(question_text, options)
            self._save_questions_to_json({'type': 'radio', 'question': question_text, 'answer': answer})
            self.all_data = self._load_questions_from_json()
            self.job_application.save_application_data({'type': 'radio', 'question': question_text, 'answer': answer})
            self._select_radio(radios, answer)
            logger.debug("Selected new radio answer")
            return True

        return False

    def _find_and_handle_textbox_question(self, section: WebElement) -> bool:
        logger.debug("Searching for text fields in the section.")
        text_fields = section.find_elements(By.TAG_NAME, 'input') + section.find_elements(By.TAG_NAME, 'textarea')

        if text_fields:
            text_field = text_fields[0]
            question_text = section.find_element(By.TAG_NAME, 'label').text.lower().strip()
            logger.debug(f"Found text field with label: {question_text}")

            is_numeric = self._is_numeric_field(text_field)
            logger.debug(f"Is the field numeric? {'Yes' if is_numeric else 'No'}")

            question_type = 'numeric' if is_numeric else 'textbox'

            # Check if it's a cover letter field (case-insensitive)
            is_cover_letter = 'cover letter' in question_text.lower()
            logger.debug(f"question: {question_text}")
            # Look for existing answer if it's not a cover letter field
            existing_answer = None

            if not is_cover_letter:
                current_question_sanitized = self._sanitize_text(question_text)

                for item in self.all_data:
                    if item['question'] == current_question_sanitized and item.get('type') == question_type:
                        existing_answer = item['answer']
                        logger.debug(f"Found existing answer: {existing_answer}")
                        break

            if existing_answer and not is_cover_letter:
                answer = existing_answer
                logger.debug(f"Using existing answer: {answer}")
            else:
                if is_numeric:
                    answer = self.gpt_answerer.answer_question_numeric(question_text)
                    logger.debug(f"Generated numeric answer: {answer}")
                else:
                    answer = self.gpt_answerer.answer_question_textual_wide_range(question_text)
                    logger.debug(f"Generated textual answer: {answer}")

            self._enter_text(text_field, answer)
            logger.debug("Entered answer into the textbox.")
            self.job_application.save_application_data({'type': question_type, 'question': question_text, 'answer': answer})

            # Save non-cover letter answers
            if not is_cover_letter and not existing_answer:
                self._save_questions_to_json({'type': question_type, 'question': question_text, 'answer': answer})
                self.all_data = self._load_questions_from_json()
                logger.debug("Saved non-cover letter answer to JSON.")

            time.sleep(1)
            text_field.send_keys(Keys.ARROW_DOWN)
            text_field.send_keys(Keys.ENTER)
            logger.debug("Selected first option from the dropdown.")
            return True

        logger.debug("No text fields found in the section.")
        return False

    def _find_and_handle_date_question(self, section: WebElement) -> bool:
        date_fields = section.find_elements(By.CLASS_NAME, 'artdeco-datepicker__input ')

        if date_fields:
            date_field = date_fields[0]
            question_text = section.text.lower()
            answer_date = self.gpt_answerer.answer_question_date()
            answer_text = answer_date.strftime("%Y-%m-%d")
            existing_answer = None
            current_question_sanitized = self._sanitize_text(question_text)

            for item in self.all_data:
                if current_question_sanitized in item['question'] and item['type'] == 'date':
                    existing_answer = item
                    break

            if existing_answer:
                self._enter_text(date_field, existing_answer['answer'])
                logger.debug("Entered existing date answer")
                self.job_application.save_application_data(existing_answer)
                return True

            self._save_questions_to_json({'type': 'date', 'question': question_text, 'answer': answer_text})
            self.all_data = self._load_questions_from_json()
            self.job_application.save_application_data({'type': 'date', 'question': question_text, 'answer': answer_text})
            self._enter_text(date_field, answer_text)
            logger.debug("Entered new date answer")
            return True
        return False

    def _find_and_handle_dropdown_question(self, section: WebElement) -> bool:
        try:
            question = section.find_element(By.CLASS_NAME, 'jobs-easy-apply-form-element')

            dropdowns = question.find_elements(By.TAG_NAME, 'select')

            if not dropdowns:
                dropdowns = section.find_elements(By.CSS_SELECTOR, '[data-test-text-entity-list-form-select]')

            if dropdowns:
                dropdown = dropdowns[0]
                select = ui.Select(dropdown)
                select = ui.Select(dropdown)
                options = [option.text for option in select.options]

                logger.debug(f"Dropdown options found: {options}")

                question_text = question.find_element(
                    By.TAG_NAME, 'label').text.lower()
                logger.debug(f"Processing dropdown or combobox question: {
                             question_text}")
                question_text = question.find_element(
                    By.TAG_NAME, 'label').text.lower()
                logger.debug(f"Processing dropdown or combobox question: {
                             question_text}")

                current_selection = select.first_selected_option.text
                logger.debug(f"Current selection: {current_selection}")

                existing_answer = None
                current_question_sanitized = self._sanitize_text(question_text)

                for item in self.all_data:
                    if current_question_sanitized in item['question'] and item['type'] == 'dropdown':
                        existing_answer = item['answer']
                        break

                if existing_answer:
                    logger.debug(f"Found existing answer for question '{question_text}': {existing_answer}")
                    self.job_application.save_application_data({'type': 'dropdown', 'question': question_text, 'answer': existing_answer})

                    if current_selection != existing_answer:
                        logger.debug(f"Updating selection to: {existing_answer}")
                        self._select_dropdown_option(dropdown, existing_answer)
                else:
                    logger.debug(f"No existing answer found, querying model for: {question_text}")
                    answer = self.gpt_answerer.answer_question_from_options(question_text, options)
                    self._save_questions_to_json({'type': 'dropdown', 'question': question_text, 'answer': answer})
                    self.all_data = self._load_questions_from_json()
                    self.job_application.save_application_data({'type': 'dropdown', 'question': question_text, 'answer': answer})
                    self._select_dropdown_option(dropdown, answer)
                    logger.debug(f"Selected new dropdown answer: {answer}")

                return True
            else:
                logger.debug(f"No dropdown found. Logging elements for debugging.")
                elements = section.find_elements(By.XPATH, ".//*")
                logger.debug(f"Elements found: {[element.tag_name for element in elements]}")
                return False
        except Exception as e:
            logger.warning(f"Failed to handle dropdown or combobox question: {e}", exc_info=True)
            return False

    def _is_numeric_field(self, field: WebElement) -> bool:
        field_type = field.get_attribute('type').lower()
        field_id = field.get_attribute("id").lower()
        is_numeric = 'numeric' in field_id or field_type == 'number' or ('text' == field_type and 'numeric' in field_id)
        logger.debug(f"Field type: {field_type}, Field ID: {field_id}, Is numeric: {is_numeric}")
        return is_numeric

    def _enter_text(self, element: WebElement, text: str) -> None:
        logger.debug(f"Entering text: {text}")
        element.clear()
        element.send_keys(text)

    def _select_radio(self, radios: List[WebElement], answer: str) -> None:
        logger.debug(f"Selecting radio option: {answer}")

        for radio in radios:
            if answer in radio.text.lower():
                radio.find_element(By.TAG_NAME, 'label').click()
                return

        radios[-1].find_element(By.TAG_NAME, 'label').click()

    def _select_dropdown_option(self, element: WebElement, text: str) -> None:
        logger.debug(f"Selecting dropdown option: {text}")
        select = ui.Select(element)
        select = ui.Select(element)
        select.select_by_visible_text(text)

    # TODO: un-nest the try-blocks and stop throwing general exceptions
    def _save_questions_to_json(self, question_data: dict) -> None:
        output_file = 'answers.json'
        question_data['question'] = self._sanitize_text(question_data['question'])

        logger.debug(f"Checking if question data already exists: {question_data}")

        try:
            with open(output_file, 'r+') as f:
                try:
                    data = json.load(f)

                    if not isinstance(data, list):
                        raise ValueError("JSON file format is incorrect. Expected a list of questions.")
                except json.JSONDecodeError:
                    logger.error("JSON decoding failed")
                    data = []

                should_be_saved: bool = not question_already_exists_in_data(question_data['question'], data) and not self.answer_contians_company_name(question_data['answer'])

                if should_be_saved:
                    logger.debug("New question found, appending to JSON")
                    data.append(question_data)
                    f.seek(0)
                    json.dump(data, f, indent=4)
                    f.truncate()
                    logger.debug("Question data saved successfully to JSON")
                else:
                    logger.debug("Question already exists, skipping save")
        except FileNotFoundError:
            logger.warning("JSON file not found, creating new file")
            with open(output_file, 'w') as f:
                json.dump([question_data], f, indent=4)
            logger.debug("Question data saved successfully to new JSON file")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Error saving questions data to JSON file: {tb_str}")
            raise Exception(f"Error saving questions data to JSON file: \nTraceback:\n{tb_str}")

    def _sanitize_text(self, text: str) -> str:
        sanitized_text = text.lower().strip().replace('"', '').replace('\\', '')
        sanitized_text = re.sub(r'[\x00-\x1F\x7F]', '', sanitized_text).replace('\n', ' ').replace('\r', '').rstrip(',')
        logger.debug(f"Sanitized text: {sanitized_text}")
        return sanitized_text

    def _find_existing_answer(self, question_text):
        for item in self.all_data:
            if self._sanitize_text(item['question']) == self._sanitize_text(question_text):
                return item
        return None

    def answer_contians_company_name(self, answer: Any) -> bool:
        return isinstance(answer, str) and not self.current_job.company is None and self.current_job.company in answer
    def answer_contians_company_name(self, answer: Any) -> bool:
        return isinstance(answer, str) and not self.current_job.company is None and self.current_job.company in answer
