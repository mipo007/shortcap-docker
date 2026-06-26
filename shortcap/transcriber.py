import openai
from openai._types import FileTypes
from .config import get_openai_api_key, get_openai_api_base
import logging

# 获取 logger
logger = logging.getLogger('shortcap.transcriber')

openai.api_key = get_openai_api_key()
openai.api_base = get_openai_api_base()

class TranscriptionError(Exception):
    """Custom exception class for handling errors during transcription"""
    pass

def transcribe_with_api(
    audio_file: FileTypes,
    prompt: str | None = None
):
    """
    Transcribe an audio file using the OpenAI Whisper API
    """
    try:
        client = openai.OpenAI(api_key=get_openai_api_key(), base_url=get_openai_api_base())
        logger.info("Initiating transcription with OpenAI Whisper API")
        transcript = client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=open(audio_file, "rb"),
            response_format="verbose_json",
            timestamp_granularities=["segment", "word"],
            prompt=prompt,
        )
        logger.info("Transcription with API completed successfully")
    except openai.APIError as e:
        logger.error(f"OpenAI API error: {str(e)}")
        raise TranscriptionError(f"OpenAI API call failed: {str(e)}")
    except openai.APIConnectionError as e:
        logger.error(f"Unable to connect to OpenAI API: {str(e)}")
        raise TranscriptionError(f"Unable to connect to OpenAI API: {str(e)}")
    except openai.RateLimitError as e:
        logger.error(f"OpenAI API rate limit error: {str(e)}")
        raise TranscriptionError(f"OpenAI API rate limit: {str(e)}")
    except Exception as e:
        logger.error(f"Unknown error occurred during transcription: {str(e)}")
        raise TranscriptionError(f"Unknown error occurred during transcription: {str(e)}")

    try:
        logger.debug("Processing API response")
        # Add space to beginning of words
        # to match local Whisper format
        modified_words = []
        for word in transcript.words:
            modified_words.append({
                "word": " " + word.word,
                "start": word.start,
                "end": word.end
            })

        # Return response in same format
        # as local Whisper format
        return [{
            "start": transcript.segments[0].start,
            "end": transcript.segments[-1].end,
            "words": modified_words,
        }]
    except AttributeError as e:
        logger.error(f"API response format error: {str(e)}")
        raise TranscriptionError(f"API response format error: {str(e)}")
    except IndexError as e:
        logger.error(f"Missing segment information in API response: {str(e)}")
        raise TranscriptionError(f"Missing segment information in API response: {str(e)}")

def transcribe_locally(
    audio_file: str,
    prompt: str | None = None
):
    """
    Transcribe an audio file using the local Whisper package
    (https://pypi.org/project/openai-whisper/)
    """
    try:
        import whisper
    except ImportError:
        logger.error("Unable to import whisper module. Make sure openai-whisper is installed.")
        raise TranscriptionError("Unable to import whisper module. Make sure openai-whisper is installed.")

    try:
        logger.info("Loading Whisper model")
        model = whisper.load_model("base")
    except Exception as e:
        logger.error(f"Error loading Whisper model: {str(e)}")
        raise TranscriptionError(f"Failed to load Whisper model: {str(e)}")

    try:
        logger.info("Starting local transcription")
        transcription = model.transcribe(
            audio=audio_file,
            word_timestamps=True,
            fp16=False,
            initial_prompt=prompt,
        )
        logger.info("Local transcription completed successfully")
    except Exception as e:
        logger.error(f"Error during local transcription: {str(e)}")
        raise TranscriptionError(f"Local transcription failed: {str(e)}")

    return transcription["segments"]
