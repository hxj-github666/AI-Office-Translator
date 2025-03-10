import os
import shutil
import json
from config.log_config import app_logger


from llmWrapper.ollama_wrapper import translate_text
from textProcessing.text_separator import stream_segment_json
from translator.load_prompt import load_prompt
from .translation_checker import SRC_JSON_PATH, RESULT_JSON_PATH, FAILED_JSON_PATH, process_translation_results, clean_json, check_and_sort_translations


class DocumentTranslator:
    def __init__(self, input_file_path, model, use_online, api_key, src_lang, dst_lang, max_token, previous_text=None):
        self.input_file_path = input_file_path
        self.model = model
        self.src_lang = src_lang
        self.dst_lang = dst_lang
        self.max_token = max_token
        self.previous_text = previous_text
        self.use_online = use_online
        self.api_key = api_key
        self.failed_status = True

        # Load translation prompts
        self.system_prompt, self.user_prompt, self.previous_prompt, self.previous_text_default = load_prompt(src_lang, dst_lang)
        if self.previous_text is None:
            self.previous_text = self.previous_text_default

    def extract_content_to_json(self):
        """Abstract method: Extract document content to JSON."""
        raise NotImplementedError

    def write_translated_json_to_file(self, json_path, translated_json_path):
        """Abstract method: Write the translated JSON content back to the file."""
        raise NotImplementedError

    def translate_content(self, progress_callback):
        app_logger.info("Segmenting JSON content...")
        stream_generator = stream_segment_json(
            SRC_JSON_PATH,
            self.max_token,
            self.system_prompt,
            self.user_prompt,
            self.previous_prompt,
            self.previous_text,
        )
        
        if stream_generator is None:
            app_logger.warning("Failed to generate segments.")
            return

        app_logger.info("Translating segments...")
        combined_previous_texts = []
        for segment, segment_progress in stream_generator():
            last_valid_translated_text = None

            for retry_count in range(2):
                try:
                    translated_text = translate_text(
                        segment, self.previous_text, self.model, self.use_online, self.api_key,
                        self.system_prompt, self.user_prompt, self.previous_prompt
                    )

                    if not translated_text:
                        app_logger.warning("translate_text returned empty or None.")
                        raise ValueError("Empty translation result.")
                    
                    process_translation_results(segment, translated_text)
                    
                    cleaned_text = clean_json(translated_text)
                    translated_lines = cleaned_text.splitlines()

                    if len(translated_lines) >= 4:
                        last_3_entries = translated_lines[-4:-1]
                        self.previous_text = "\n".join(last_3_entries)
                    else:
                        app_logger.warning("Translated text does not have enough lines to update previous_text,use Default ones")
                        self.previous_text = self.previous_text_default

                    combined_previous_texts.append(translated_text)
                    break

                except (json.JSONDecodeError, ValueError, RuntimeError) as e:
                    app_logger.warning(f"Error encountered: {e}. Retrying ({retry_count + 1}/2)...")
                    last_valid_translated_text = translated_text
   
                    if retry_count == 1:
                        app_logger.warning(f"All retries failed for segment: {segment}. Marking it as failed.")
                        self._mark_segment_as_failed(segment)

            else:
                if last_valid_translated_text:
                    app_logger.warning("Saving last valid translation despite errors.")
                    combined_previous_texts.append(last_valid_translated_text)

            if progress_callback:
                progress_callback(segment_progress, desc="Translating...Please wait.")
                app_logger.info(f"Progress: {segment_progress * 100:.2f}%")
        
        # Maximum number of retries
        for _ in range(3):
            if not self.failed_status:
                break
            self.failed_status = self.retranslate_failed_content(combined_previous_texts, progress_callback)
        
        # Line by line translate
        if self.failed_status:
            app_logger.warning("Final attempt: Retranslating failed segments line by line...")
            self._retranslate_failed_lines(progress_callback)

    def retranslate_failed_content(self, combined_previous_texts, progress_callback):
        app_logger.info("Retrying translation for failed segments...")
        if not os.path.exists(FAILED_JSON_PATH):
            app_logger.info("No failed segments to retranslate. Skipping this step.")
            return False

        # Check if file is empty or contains an empty JSON array
        with open(FAILED_JSON_PATH, 'r', encoding='utf-8') as f:
            try:
                data = json.load(f)
                if not data:  # If the JSON is an empty list or dict
                    app_logger.info("No failed segments to retranslate. Skipping this step.")
                    return False
            except json.JSONDecodeError:
                app_logger.error("Failed to decode JSON. Skipping this step.")
                return False

        stream_generator_failed = stream_segment_json(
            FAILED_JSON_PATH,
            self.max_token,
            self.system_prompt,
            self.user_prompt,
            self.previous_prompt,
            self.previous_text
        )
        if stream_generator_failed is None:
            app_logger.info("All text has been translated.")
            return False

        if os.path.exists(FAILED_JSON_PATH):
            with open(FAILED_JSON_PATH, "w", encoding="utf-8") as f:
                f.write("[]")
            app_logger.info("Cleared temp/dst_translated_failed.json")

        has_translated_success = False
        for segment, segment_progress in stream_generator_failed():
            last_valid_translated_text = None
            for retry_count in range(2):
                try:
                    translated_text = translate_text(
                        segment,
                        self.previous_text,
                        self.model,
                        self.use_online,
                        self.api_key,
                        self.system_prompt,
                        self.user_prompt,
                        self.previous_prompt
                    )

                    if process_translation_results(segment, translated_text):
                        has_translated_success = True
                    app_logger.info("Segment retranslated successfully.")
                    
                    last_3_entries = clean_json(translated_text).splitlines()[-4:-1]
                    self.previous_text = "\n".join(last_3_entries)
                    combined_previous_texts.append(translated_text)
                    break

                except (json.JSONDecodeError, ValueError, RuntimeError) as e:
                    app_logger.warning(f"Error encountered: {e}. Retrying ({retry_count + 1}/2)...")
                    last_valid_translated_text = translated_text

            else:
                if last_valid_translated_text:
                    app_logger.warning("Saving last valid translation despite errors.")
                    combined_previous_texts.append(last_valid_translated_text)

            if progress_callback:
                progress_callback(segment_progress, desc="Missing detected! Re-translating...")
                app_logger.info(f"Progress: {segment_progress * 100:.2f}%")
        return has_translated_success

    def _retranslate_failed_lines(self, progress_callback):
        """
        Retranslate failed lines individually, with a max retry of 2 times.
        """

        if not os.path.exists(FAILED_JSON_PATH):
            return
        try:
            with open(FAILED_JSON_PATH, "r", encoding="utf-8") as f:
                failed_segments = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            app_logger.error(f"Failed to read or decode FAILED_JSON_PATH: {e}")
            return
        if not failed_segments:
            app_logger.info("No failed segments left for line-by-line translation.")
            return

        open(FAILED_JSON_PATH, "w", encoding="utf-8").write("[]")

        new_failed_segments = []

        for entry in failed_segments:
            count = entry["count"]
            text_value = entry["value"]
            text_failed = self._convert_failed_segments_to_json(entry)
            last_valid_line_translation = None
            for retry in range(2):
                try:
                    translated_line = translate_text(
                        text_failed,
                        self.previous_text,
                        self.model,
                        self.use_online,
                        self.api_key,
                        self.system_prompt,
                        self.user_prompt,
                        self.previous_prompt
                    )

                    process_translation_results(text_failed, translated_line)
                    last_valid_line_translation = translated_line.strip()

                    lines = last_valid_line_translation.splitlines()
                    if len(lines) >= 3:
                        self.previous_text = "\n".join(lines[-3:])
                    else:
                        self.previous_text = self.previous_text_default

                    break
                except (json.JSONDecodeError, ValueError, RuntimeError) as e:
                    app_logger.warning(f"Error in line {count}: {e}. Retrying ({retry + 1}/2)...")

            else:
                if last_valid_line_translation:
                    app_logger.info(f"Saving last valid translation.")
                else:
                    new_failed_segments.append({
                        "count": count,
                        "value": text_value
                    })
            if progress_callback:
                progress_callback(0, desc=f"Line-by-line retranslation...")
        if new_failed_segments:
            with open(FAILED_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(new_failed_segments, f, ensure_ascii=False, indent=4)
            app_logger.warning("Some lines still failed after all retries. Check FAILED_JSON_PATH.")
        else:
            app_logger.info("All lines retranslated successfully in line-by-line mode!")

    def _convert_failed_segments_to_json(self, failed_segments):
        converted_json = {failed_segments["count"]: failed_segments["value"]}
        return json.dumps(converted_json, indent=4, ensure_ascii=False)

    def _clear_temp_folder(self):
        temp_folder = "temp"
        if os.path.exists(temp_folder):
            app_logger.info("Clearing temp folder...")
            shutil.rmtree(temp_folder)
        os.makedirs(temp_folder)
    
    def _mark_segment_as_failed(self, segment):
        """将失败段落标记为 {count, value} 对并存入 FAILED_JSON_PATH。"""
        
        if not os.path.exists(FAILED_JSON_PATH):
            with open(FAILED_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump([], f)

        with open(FAILED_JSON_PATH, "r+", encoding="utf-8") as f:
            try:
                failed_segments = json.load(f)
            except json.JSONDecodeError:
                failed_segments = []

            try:
                clean_segment = clean_json(segment)
                segment_dict = json.loads(clean_segment)
            except json.JSONDecodeError as e:
                app_logger.error(f"Failed to decode JSON segment: {segment}. Error: {e}")
                return
            for count, value in segment_dict.items():
                failed_segments.append({
                    "count": int(count),
                    "value": value.strip()
                })
            f.seek(0)
            json.dump(failed_segments, f, ensure_ascii=False, indent=4)

    def process(self, file_name, file_extension, progress_callback=None):
        self._clear_temp_folder()

        app_logger.info("Extracting content to JSON...")
        if progress_callback:
            progress_callback(0, desc="Extracting text, please wait...")
        json_path = self.extract_content_to_json(progress_callback)

        app_logger.info("Translating content...")
        if progress_callback:
            progress_callback(0, desc="Translating, please wait...")
        self.translate_content(progress_callback)

        if progress_callback:
            progress_callback(0, desc="Checking for errors...")
        missing_counts = check_and_sort_translations()

        app_logger.info("Writing translated content to file...")
        if progress_callback:
            progress_callback(0, desc="Translation completed, new file being generated...")
        self.write_translated_json_to_file(json_path, RESULT_JSON_PATH,progress_callback)

        result_folder = "result" 
        base_name = os.path.basename(file_name)
        final_output_path = os.path.join(result_folder, f"{base_name}_translated{file_extension}")
        return final_output_path,missing_counts