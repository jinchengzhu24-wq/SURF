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
    private const string PlayerNamePrefsKey = "SokobanSurveyPlayerName";
    private const int RequiredAnswerCount = 3;

    [Header("Survey")]
    public string surveyId = "post_play_survey";
    public string surveyTitle = "Questionnaire";
    public string nextSceneName = "";

    [Header("Scene UI")]
    public Button submitButton;
    public InputField playerNameInput;

    [Header("External Questionnaire")]
    [Tooltip("When set, this scene opens the survey in the browser and uses Submit as Leave.")]
    public string externalSurveyUrl = "";
    public Button externalSurveyLinkButton;

    [Header("Player Name Input")]
    public bool requirePlayerName = true;
    public bool startsSurveyPair;
    public Font pixelFont;
    public string playerNamePlaceholder = "Enter your nickname";
    [Min(0)]
    public int playerNameCharacterLimit = 24;

    [Header("Backend")]
    public string backendBaseUrl = DefaultBackendBaseUrl;
    public int requestTimeoutSeconds = 5;
    public bool logSurveyEvents = true;

    private readonly Dictionary<int, QuestionnaireOptionButton> selectedOptions =
        new Dictionary<int, QuestionnaireOptionButton>();
    private QuestionnaireOptionButton[] optionButtons = new QuestionnaireOptionButton[0];
    private QuestionnaireScoreSlider[] scoreSliders = new QuestionnaireScoreSlider[0];
    private float startedAt;
    private bool isSubmitting;
    private bool externalSurveyOpened;

    private void Awake()
    {
        if (startsSurveyPair)
        {
            BeginSurveyPair();
        }

        startedAt = Time.realtimeSinceStartup;
    }

    private void Start()
    {
        ResolveSceneReferences();
        ConfigurePlayerNameInput();
        WireButtons();
        UpdateSubmitState();
    }

    private void OnDestroy()
    {
        if (playerNameInput != null)
        {
            playerNameInput.onValueChanged.RemoveListener(OnPlayerNameChanged);
        }
    }

    private void ResolveSceneReferences()
    {
        optionButtons = FindObjectsOfType<QuestionnaireOptionButton>();
        scoreSliders = FindObjectsOfType<QuestionnaireScoreSlider>();

        if (submitButton == null)
        {
            GameObject submitObject = GameObject.Find("SubmitButton");

            if (submitObject != null)
            {
                submitButton = submitObject.GetComponent<Button>();
            }
        }

        if (playerNameInput == null)
        {
            GameObject inputObject = GameObject.Find("InputField");

            if (inputObject != null)
            {
                playerNameInput = inputObject.GetComponent<InputField>();
            }
        }

        if (playerNameInput == null)
        {
            playerNameInput = FindObjectOfType<InputField>();
        }
    }

    private void WireButtons()
    {
        for (int i = 0; i < scoreSliders.Length; i++)
        {
            scoreSliders[i].Prepare();
        }

        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];
            option.ResolveReferences();
            option.Button.onClick.RemoveAllListeners();
            option.Button.onClick.AddListener(() => SelectOption(option));
        }

        RefreshSelectionVisuals();

        if (submitButton != null)
        {
            submitButton.onClick.RemoveAllListeners();
            submitButton.onClick.AddListener(
                UsesExternalSurvey ? (UnityEngine.Events.UnityAction)Leave : Submit
            );
        }

        if (externalSurveyLinkButton != null)
        {
            externalSurveyLinkButton.onClick.RemoveAllListeners();
            externalSurveyLinkButton.onClick.AddListener(OpenExternalSurvey);
            externalSurveyLinkButton.interactable = !externalSurveyOpened;
        }

        if (playerNameInput != null)
        {
            playerNameInput.onValueChanged.RemoveListener(OnPlayerNameChanged);
            playerNameInput.onValueChanged.AddListener(OnPlayerNameChanged);
        }
    }

    private void ConfigurePlayerNameInput()
    {
        if (playerNameInput == null)
        {
            return;
        }

        playerNameInput.contentType = InputField.ContentType.Standard;
        playerNameInput.lineType = InputField.LineType.SingleLine;
        playerNameInput.characterLimit = Mathf.Max(0, playerNameCharacterLimit);
        playerNameInput.caretWidth = 2;
        playerNameInput.customCaretColor = true;
        playerNameInput.caretColor = new Color(0.08f, 0.12f, 0.18f, 1f);
        playerNameInput.selectionColor = new Color(0.24f, 0.48f, 0.9f, 0.35f);

        if (playerNameInput.textComponent != null)
        {
            ApplyInputTextStyle(
                playerNameInput.textComponent,
                Color.black,
                FontStyle.Normal
            );
        }

        Text placeholderText = playerNameInput.placeholder as Text;

        if (placeholderText != null)
        {
            placeholderText.text = IsBlank(playerNamePlaceholder)
                ? "Enter your nickname"
                : playerNamePlaceholder;
            ApplyInputTextStyle(
                placeholderText,
                new Color(0f, 0f, 0f, 0.45f),
                FontStyle.Normal
            );
        }

        Image background = playerNameInput.GetComponent<Image>();

        if (background != null)
        {
            background.color = new Color(0.96f, 0.98f, 1f, 1f);
        }

        playerNameInput.ForceLabelUpdate();
    }

    private void ApplyInputTextStyle(Text text, Color color, FontStyle fontStyle)
    {
        if (pixelFont != null)
        {
            text.font = pixelFont;
        }

        text.color = color;
        text.fontStyle = fontStyle;
        text.alignment = TextAnchor.MiddleLeft;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        text.supportRichText = false;
    }

    private void SelectOption(QuestionnaireOptionButton selectedOption)
    {
        selectedOptions[selectedOption.questionIndex] = selectedOption;
        RefreshSelectionVisuals();
        UpdateSubmitState();
    }

    private void RefreshSelectionVisuals()
    {
        for (int i = 0; i < optionButtons.Length; i++)
        {
            QuestionnaireOptionButton option = optionButtons[i];
            QuestionnaireOptionButton selectedOption;
            bool selected = selectedOptions.TryGetValue(option.questionIndex, out selectedOption)
                && selectedOption.optionIndex == option.optionIndex;
            option.SetSelected(selected);
        }
    }

    private void UpdateSubmitState()
    {
        if (submitButton != null)
        {
            submitButton.interactable = UsesExternalSurvey
                ? externalSurveyOpened
                : !isSubmitting && IsComplete();
        }
    }

    public void OpenExternalSurvey()
    {
        if (externalSurveyOpened)
        {
            return;
        }

        if (!TryGetHttpUrl(externalSurveyUrl, out string surveyUrl))
        {
            Debug.LogError("QuestionnaireController: External survey URL must use HTTP or HTTPS.");
            return;
        }

        Application.OpenURL(surveyUrl);
        externalSurveyOpened = true;

        if (externalSurveyLinkButton != null)
        {
            externalSurveyLinkButton.interactable = false;
        }

        UpdateSubmitState();
    }

    public void Leave()
    {
        if (!UsesExternalSurvey || !externalSurveyOpened || isSubmitting)
        {
            return;
        }

        OnlineMatchContext.ClearPendingPostMatchSurvey();
        SceneManager.LoadScene(IsBlank(nextSceneName) ? "Menu" : nextSceneName);
    }

    private bool IsComplete()
    {
        if (scoreSliders.Length > 0)
        {
            HashSet<int> scoreQuestionIndexes = new HashSet<int>();

            for (int i = 0; i < scoreSliders.Length; i++)
            {
                QuestionnaireScoreSlider scoreSlider = scoreSliders[i];

                if (!scoreSlider.HasValidScore)
                {
                    return false;
                }

                scoreQuestionIndexes.Add(scoreSlider.questionIndex);
            }

            bool allScoresPresent = scoreSliders.Length == RequiredAnswerCount
                && scoreQuestionIndexes.Count == RequiredAnswerCount;
            return allScoresPresent
                && (!requirePlayerName || !IsBlank(PlayerNameValue));
        }

        HashSet<int> questionIndexes = new HashSet<int>();

        for (int i = 0; i < optionButtons.Length; i++)
        {
            questionIndexes.Add(optionButtons[i].questionIndex);
        }

        int selectedQuestionCount = 0;

        foreach (int questionIndex in questionIndexes)
        {
            if (selectedOptions.ContainsKey(questionIndex))
            {
                selectedQuestionCount++;
            }
        }

        bool allOptionsSelected = questionIndexes.Count == RequiredAnswerCount
            && selectedQuestionCount == RequiredAnswerCount;

        if (!allOptionsSelected)
        {
            return false;
        }

        return !requirePlayerName || !IsBlank(PlayerNameValue);
    }

    private void OnPlayerNameChanged(string value)
    {
        UpdateSubmitState();
    }

    private void Submit()
    {
        if (isSubmitting || !IsComplete())
        {
            return;
        }

        SavePlayerName();
        StartCoroutine(SubmitRoutine());
    }

    private IEnumerator SubmitRoutine()
    {
        isSubmitting = true;
        UpdateSubmitState();

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
            if (!IsBlank(record.matchId))
            {
                OnlineMatchContext.ClearPendingPostMatchSurvey();
            }

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
        if (scoreSliders.Length > 0)
        {
            return CreateScoreResponseRecord();
        }

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

        return CreateResponseRecord(answers);
    }

    private SurveyResponseRecord CreateScoreResponseRecord()
    {
        List<QuestionnaireScoreSlider> orderedSliders =
            new List<QuestionnaireScoreSlider>(scoreSliders);
        orderedSliders.Sort(
            (left, right) => left.questionIndex.CompareTo(right.questionIndex)
        );
        SurveyAnswerRecord[] answers =
            new SurveyAnswerRecord[orderedSliders.Count];

        for (int i = 0; i < orderedSliders.Count; i++)
        {
            QuestionnaireScoreSlider scoreSlider = orderedSliders[i];
            scoreSlider.Prepare();
            int score = scoreSlider.CurrentScore;
            answers[i] = new SurveyAnswerRecord
            {
                questionIndex = scoreSlider.questionIndex,
                questionId = scoreSlider.questionId,
                questionText = scoreSlider.QuestionTextValue,
                optionIndex = score,
                optionId = "score_" + score,
                optionText = score.ToString()
            };
        }

        return CreateResponseRecord(answers);
    }

    private SurveyResponseRecord CreateResponseRecord(
        SurveyAnswerRecord[] answers)
    {
        bool isOnlinePostMatchSurvey = string.Equals(
            surveyId,
            "online_post_match_survey",
            StringComparison.Ordinal
        );
        return new SurveyResponseRecord
        {
            eventType = "survey-response",
            surveyId = IsBlank(surveyId) ? "post_play_survey" : surveyId,
            surveyTitle = IsBlank(surveyTitle) ? "Questionnaire" : surveyTitle,
            sessionId = GetOrCreateSessionId(),
            creativeIdeaId = GetCreativeIdeaId(),
            responseId = Guid.NewGuid().ToString("N"),
            playerName = PlayerNameValue,
            sceneName = SceneManager.GetActiveScene().name,
            officialRound = LevelStudyRecorder.IsOfficialRoundFlow,
            matchId = isOnlinePostMatchSurvey
                ? OnlineMatchContext.PendingSurveyMatchId
                : "",
            roomCode = isOnlinePostMatchSurvey
                ? OnlineMatchContext.PendingSurveyRoomCode
                : "",
            playerNumber = isOnlinePostMatchSurvey
                ? OnlineMatchContext.PendingSurveyPlayerNumber
                : 0,
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
        return CreativeWorkshopContext.GetOrCreateStudySessionId();
    }

    private void BeginSurveyPair()
    {
        CreativeWorkshopContext.BeginStudySession();
        PlayerPrefs.DeleteKey(PlayerNamePrefsKey);
        PlayerPrefs.Save();
    }

    private void SavePlayerName()
    {
        if (playerNameInput == null || IsBlank(playerNameInput.text))
        {
            return;
        }

        PlayerPrefs.SetString(PlayerNamePrefsKey, playerNameInput.text.Trim());
        PlayerPrefs.Save();
    }

    private string GetCreativeIdeaId()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaId)
            ? CreativeWorkshopContext.IdeaId
            : PlayerPrefs.GetString(CreativeWorkshopContext.IdeaIdPrefsKey, "");
    }

    private string PlayerNameValue
    {
        get
        {
            if (playerNameInput != null && !IsBlank(playerNameInput.text))
            {
                return playerNameInput.text.Trim();
            }

            return PlayerPrefs.GetString(PlayerNamePrefsKey, "").Trim();
        }
    }

    private bool IsBlank(string value)
    {
        return string.IsNullOrEmpty(value) || value.Trim().Length == 0;
    }

    private bool UsesExternalSurvey
    {
        get { return !IsBlank(externalSurveyUrl); }
    }

    private static bool TryGetHttpUrl(string value, out string resolvedUrl)
    {
        resolvedUrl = string.Empty;

        if (string.IsNullOrWhiteSpace(value))
        {
            return false;
        }

        Uri uri;
        if (!Uri.TryCreate(value.Trim(), UriKind.Absolute, out uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            return false;
        }

        resolvedUrl = uri.AbsoluteUri;
        return true;
    }
}

[Serializable]
public class SurveyResponseRecord
{
    public string eventType;
    public string surveyId;
    public string surveyTitle;
    public string sessionId;
    public string creativeIdeaId;
    public string responseId;
    public string playerName;
    public string sceneName;
    public bool officialRound;
    public string matchId;
    public string roomCode;
    public int playerNumber;
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
