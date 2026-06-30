using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using TMPro;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.TextCore.LowLevel;
using UnityEngine.UI;

public class QuestionnaireController : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "http://111.231.136.4:8000";
    private const string SurveyResponsePath = "/record-survey-response";
    private const string SessionPrefsKey = "SokobanSurveySessionId";
    private const int RequiredAnswerCount = 3;

    [Header("Survey")]
    public string surveyId = "post_play_survey";
    public string surveyTitle = "Questionnaire";
    public string nextSceneName = "";

    [Header("Scene UI")]
    public Button submitButton;
    public Text statusText;
    public TMP_InputField playerNameInput;

    [Header("Player Name Input")]
    public Font pixelFont;
    public TMP_FontAsset pixelTMPFontAsset;
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
    private float startedAt;
    private bool isSubmitting;

    private void Awake()
    {
        startedAt = Time.realtimeSinceStartup;
    }

    private void Start()
    {
        ResolveSceneReferences();
        ConfigurePlayerNameInput();
        WireButtons();
        SetStatus("");
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

        if (playerNameInput == null)
        {
            GameObject inputObject = GameObject.Find("InputField (TMP)");

            if (inputObject != null)
            {
                playerNameInput = inputObject.GetComponent<TMP_InputField>();
            }
        }

        if (playerNameInput == null)
        {
            playerNameInput = FindObjectOfType<TMP_InputField>();
        }
    }

    private void WireButtons()
    {
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
            submitButton.onClick.AddListener(Submit);
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

        TMP_FontAsset previousFontAsset = playerNameInput.fontAsset;
        TMP_FontAsset inputFontAsset = ResolvePixelTMPFontAsset();

        if (inputFontAsset != null)
        {
            AddFontFallback(inputFontAsset, previousFontAsset);
            playerNameInput.fontAsset = inputFontAsset;
        }

        playerNameInput.contentType = TMP_InputField.ContentType.Standard;
        playerNameInput.lineType = TMP_InputField.LineType.SingleLine;
        playerNameInput.characterLimit = Mathf.Max(0, playerNameCharacterLimit);
        playerNameInput.richText = false;
        playerNameInput.onFocusSelectAll = false;
        playerNameInput.resetOnDeActivation = false;
        playerNameInput.caretWidth = 2;
        playerNameInput.customCaretColor = true;
        playerNameInput.caretColor = new Color(0.08f, 0.12f, 0.18f, 1f);
        playerNameInput.selectionColor = new Color(0.24f, 0.48f, 0.9f, 0.35f);

        if (playerNameInput.textComponent != null)
        {
            ApplyInputTextStyle(
                playerNameInput.textComponent,
                inputFontAsset,
                new Color(0.08f, 0.12f, 0.18f, 1f),
                FontStyles.Normal
            );
        }

        TMP_Text placeholderText = playerNameInput.placeholder as TMP_Text;

        if (placeholderText != null)
        {
            placeholderText.text = IsBlank(playerNamePlaceholder)
                ? "Enter your nickname"
                : playerNamePlaceholder;
            ApplyInputTextStyle(
                placeholderText,
                inputFontAsset,
                new Color(0.08f, 0.12f, 0.18f, 0.45f),
                FontStyles.Normal
            );
        }

        Image background = playerNameInput.GetComponent<Image>();

        if (background != null)
        {
            background.color = new Color(0.96f, 0.98f, 1f, 1f);
        }

        playerNameInput.ForceLabelUpdate();
    }

    private TMP_FontAsset ResolvePixelTMPFontAsset()
    {
        if (pixelTMPFontAsset != null)
        {
            return pixelTMPFontAsset;
        }

        if (pixelFont == null)
        {
            return null;
        }

#if UNITY_EDITOR
        if (!Application.isPlaying)
        {
            return null;
        }
#endif

        pixelTMPFontAsset = TMP_FontAsset.CreateFontAsset(
            pixelFont,
            96,
            8,
            GlyphRenderMode.SDFAA,
            1024,
            1024,
            AtlasPopulationMode.Dynamic,
            true
        );

        if (pixelTMPFontAsset == null)
        {
            Debug.LogWarning("Unable to create a TMP font asset from " + pixelFont.name + ".");
            return null;
        }

        pixelTMPFontAsset.name = pixelFont.name + " TMP Runtime";
        PrepareFontAtlas(pixelTMPFontAsset);
        return pixelTMPFontAsset;
    }

    private void ApplyInputTextStyle(TMP_Text text, TMP_FontAsset fontAsset, Color color, FontStyles fontStyle)
    {
        if (fontAsset != null)
        {
            text.font = fontAsset;
        }

        text.color = color;
        text.fontStyle = fontStyle;
        text.alignment = TextAlignmentOptions.Left;
        text.enableWordWrapping = false;
        text.overflowMode = TextOverflowModes.Truncate;
    }

    private void PrepareFontAtlas(TMP_FontAsset fontAsset)
    {
        if (fontAsset == null)
        {
            return;
        }

        if (fontAsset.atlasTextures != null)
        {
            for (int i = 0; i < fontAsset.atlasTextures.Length; i++)
            {
                Texture2D atlasTexture = fontAsset.atlasTextures[i];

                if (atlasTexture != null)
                {
                    atlasTexture.filterMode = FilterMode.Bilinear;
                    atlasTexture.wrapMode = TextureWrapMode.Clamp;
                }
            }
        }

        fontAsset.TryAddCharacters(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _-.,!?:'\"()[]"
        );
    }

    private void AddFontFallback(TMP_FontAsset fontAsset, TMP_FontAsset fallbackFontAsset)
    {
        if (fontAsset == null || fallbackFontAsset == null || fontAsset == fallbackFontAsset)
        {
            return;
        }

        if (fontAsset.fallbackFontAssetTable == null)
        {
            fontAsset.fallbackFontAssetTable = new List<TMP_FontAsset>();
        }

        if (!fontAsset.fallbackFontAssetTable.Contains(fallbackFontAsset))
        {
            fontAsset.fallbackFontAssetTable.Add(fallbackFontAsset);
        }
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

        return !IsBlank(PlayerNameValue);
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
            playerName = PlayerNameValue,
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

    private string PlayerNameValue
    {
        get
        {
            return playerNameInput != null && playerNameInput.text != null
                ? playerNameInput.text.Trim()
                : "";
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
    public string playerName;
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
