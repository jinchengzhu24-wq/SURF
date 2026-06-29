using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class QuestionnaireController : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "http://111.231.136.4:8000";
    private const string SurveyResponsePath = "/record-survey-response";
    private const string SessionPrefsKey = "SokobanSurveySessionId";

    [Header("Survey")]
    public string surveyId = "post_play_survey";
    public string surveyTitle = "Questionnaire";
    public string nextSceneName = "";

    [Header("Scene UI")]
    public Button submitButton;
    public Text statusText;

    [Header("Backend")]
    public string backendBaseUrl = DefaultBackendBaseUrl;
    public int requestTimeoutSeconds = 5;
    public bool logSurveyEvents = true;

    private readonly Dictionary<int, QuestionnaireOptionButton> selectedOptions =
        new Dictionary<int, QuestionnaireOptionButton>();
    private QuestionnaireOptionButton[] optionButtons = new QuestionnaireOptionButton[0];
    private float startedAt;
    private bool isSubmitting;

    private void Awake()
    {
        startedAt = Time.realtimeSinceStartup;
    }

    private void Start()
    {
        ResolveSceneReferences();
        WireButtons();
        SetStatus("");
        UpdateSubmitState();
    }

    private void ResolveSceneReferences()
    {
        optionButtons = FindObjectsOfType<QuestionnaireOptionButton>();

        if (submitButton == null)
        {
            GameObject submitObject = GameObject.Find("SubmitButton");

            if (submitObject != null)
            {
                submitButton = submitObject.GetComponent<Button>();
            }
        }

        if (statusText == null)
        {
            GameObject statusObject = GameObject.Find("StatusText");

            if (statusObject != null)
            {
                statusText = statusObject.GetComponent<Text>();
            }
        }
    }

    private void WireButtons()
    {
        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];
            option.ResolveReferences();
            option.SetSelected(false);
            option.Button.onClick.RemoveAllListeners();
            option.Button.onClick.AddListener(() => SelectOption(option));
        }

        if (submitButton != null)
        {
            submitButton.onClick.RemoveAllListeners();
            submitButton.onClick.AddListener(Submit);
        }
    }

    private void SelectOption(QuestionnaireOptionButton selectedOption)
    {
        selectedOptions[selectedOption.questionIndex] = selectedOption;

        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];
            bool selected = option.questionIndex == selectedOption.questionIndex
                && option.optionIndex == selectedOption.optionIndex;
            option.SetSelected(selected);
        }

        UpdateSubmitState();
    }

    private void UpdateSubmitState()
    {
        if (submitButton != null)
        {
            submitButton.interactable = !isSubmitting && IsComplete();
        }
    }

    private bool IsComplete()
    {
        HashSet<int> questionIndexes = new HashSet<int>();

        for (int i = 0; i < optionButtons.Length; i++)
        {
            questionIndexes.Add(optionButtons[i].questionIndex);
        }

        return questionIndexes.Count > 0 && selectedOptions.Count >= questionIndexes.Count;
    }

    private void Submit()
    {
        if (isSubmitting || !IsComplete())
        {
            return;
        }

        StartCoroutine(SubmitRoutine());
    }

    private IEnumerator SubmitRoutine()
    {
        isSubmitting = true;
        UpdateSubmitState();
        SetStatus("Submitting...");

        SurveyResponseRecord record = CreateResponseRecord();
        string json = JsonUtility.ToJson(record);
        string url = GetBackendUrl(SurveyResponsePath);
        byte[] body = Encoding.UTF8.GetBytes(json);
        UnityWebRequest request = new UnityWebRequest(url, UnityWebRequest.kHttpVerbPOST);
        request.uploadHandler = new UploadHandlerRaw(body);
        request.downloadHandler = new DownloadHandlerBuffer();
        request.timeout = Mathf.Max(1, requestTimeoutSeconds);
        request.SetRequestHeader("Content-Type", "application/json");
        request.SetRequestHeader("Accept", "application/json");

        yield return request.SendWebRequest();

        if (request.result == UnityWebRequest.Result.Success)
        {
            SetStatus("Submitted.");

            if (logSurveyEvents)
            {
                Debug.Log("Questionnaire submitted: responseId=" + record.responseId + ", surveyId=" + record.surveyId);
            }

            if (!IsBlank(nextSceneName))
            {
                yield return new WaitForSeconds(0.75f);
                SceneManager.LoadScene(nextSceneName);
            }
        }
        else
        {
            SetStatus("Submit failed: " + request.error);
            Debug.LogWarning(
                "Questionnaire submit failed:"
                + " url=" + url
                + ", error=" + request.error
                + ", responseCode=" + request.responseCode
            );
        }

        request.Dispose();
        isSubmitting = false;
        UpdateSubmitState();
    }

    private SurveyResponseRecord CreateResponseRecord()
    {
        List<int> questionIndexes = new List<int>(selectedOptions.Keys);
        questionIndexes.Sort();
        SurveyAnswerRecord[] answers = new SurveyAnswerRecord[questionIndexes.Count];

        for (int i = 0; i < questionIndexes.Count; i++)
        {
            QuestionnaireOptionButton option = selectedOptions[questionIndexes[i]];
            option.ResolveReferences();
            answers[i] = new SurveyAnswerRecord
            {
                questionIndex = option.questionIndex,
                questionId = option.questionId,
                questionText = option.QuestionTextValue,
                optionIndex = option.optionIndex,
                optionId = option.optionId,
                optionText = option.OptionTextValue
            };
        }

        return new SurveyResponseRecord
        {
            eventType = "survey-response",
            surveyId = IsBlank(surveyId) ? "post_play_survey" : surveyId,
            surveyTitle = IsBlank(surveyTitle) ? "Questionnaire" : surveyTitle,
            sessionId = GetOrCreateSessionId(),
            responseId = Guid.NewGuid().ToString("N"),
            sceneName = SceneManager.GetActiveScene().name,
            timestamp = DateTime.UtcNow.ToString("o"),
            durationSeconds = Mathf.Round((Time.realtimeSinceStartup - startedAt) * 100f) / 100f,
            answers = answers
        };
    }

    private string GetBackendUrl(string path)
    {
        string baseUrl = string.IsNullOrEmpty(backendBaseUrl)
            ? DefaultBackendBaseUrl
            : backendBaseUrl.TrimEnd('/');

        return baseUrl + path;
    }

    private string GetOrCreateSessionId()
    {
        string sessionId = PlayerPrefs.GetString(SessionPrefsKey, "");

        if (string.IsNullOrEmpty(sessionId))
        {
            sessionId = Guid.NewGuid().ToString("N");
            PlayerPrefs.SetString(SessionPrefsKey, sessionId);
            PlayerPrefs.Save();
        }

        return sessionId;
    }

    private void SetStatus(string message)
    {
        if (statusText != null)
        {
            statusText.text = message;
        }
    }

    private bool IsBlank(string value)
    {
        return string.IsNullOrEmpty(value) || value.Trim().Length == 0;
    }
}

[Serializable]
public class SurveyResponseRecord
{
    public string eventType;
    public string surveyId;
    public string surveyTitle;
    public string sessionId;
    public string responseId;
    public string sceneName;
    public string timestamp;
    public float durationSeconds;
    public SurveyAnswerRecord[] answers;
}

[Serializable]
public class SurveyAnswerRecord
{
    public int questionIndex;
    public string questionId;
    public string questionText;
    public int optionIndex;
    public string optionId;
    public string optionText;
}
