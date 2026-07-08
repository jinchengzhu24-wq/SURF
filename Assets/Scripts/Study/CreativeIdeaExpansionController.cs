using System;
using System.Collections;
using System.Text;
using TMPro;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class CreativeIdeaExpansionController : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "http://111.231.136.4:8000";
    private const string ExpansionPath = "/expand-creative-idea";
    private const string ExpansionChoicePath = "/record-expansion-choice";

    [Header("Scene UI")]
    public Button submitButton;
    public Text statusText;
    public Text guidanceText;
    public Text originalIdeaText;
    public Text runtimeStatusText;
    public Transform optionsParent;
    public Button[] optionButtons = new Button[3];
    public Image[] optionCardImages = new Image[3];
    public Text[] optionTitleTexts = new Text[3];
    public Text[] optionDescriptionTexts = new Text[3];

    [Header("Flow")]
    public string nextSceneName = "Custom_Level";

    [Header("Backend")]
    public string backendBaseUrl = DefaultBackendBaseUrl;
    public int requestTimeoutSeconds = 25;
    public bool logExpansionEvents = true;

    private readonly Color normalCardColor = new Color(0.95f, 0.97f, 1f, 1f);
    private readonly Color selectedCardColor = new Color(0.78f, 0.88f, 1f, 1f);
    private readonly Color disabledCardColor = new Color(0.86f, 0.86f, 0.86f, 1f);

    private CreativeIdeaExpansionOption[] options = new CreativeIdeaExpansionOption[0];
    private string originalIdea = "";
    private bool isRequesting;
    private bool isContinuing;
    private int selectedOptionIndex = -1;

    private void Start()
    {
        EnsureUiArrays();
        ResolveSceneReferences();
        WireButtons();
        LoadOriginalIdea();
        RefreshStaticText();
        UpdateSubmitState();
        StartCoroutine(RequestExpansionRoutine());
    }

    private void OnDestroy()
    {
        if (submitButton != null)
        {
            submitButton.onClick.RemoveListener(ContinueToLevel);
        }

        if (optionButtons == null)
        {
            return;
        }

        for (int i = 0; i < optionButtons.Length; i++)
        {
            if (optionButtons[i] != null)
            {
                optionButtons[i].onClick.RemoveAllListeners();
            }
        }
    }

    private void EnsureUiArrays()
    {
        if (optionButtons == null || optionButtons.Length < 3)
        {
            optionButtons = new Button[3];
        }

        if (optionCardImages == null || optionCardImages.Length < 3)
        {
            optionCardImages = new Image[3];
        }

        if (optionTitleTexts == null || optionTitleTexts.Length < 3)
        {
            optionTitleTexts = new Text[3];
        }

        if (optionDescriptionTexts == null || optionDescriptionTexts.Length < 3)
        {
            optionDescriptionTexts = new Text[3];
        }
    }

    private void ResolveSceneReferences()
    {
        if (submitButton == null)
        {
            GameObject submitObject = GameObject.Find("SubmitButton");

            if (submitObject != null)
            {
                submitButton = submitObject.GetComponent<Button>();
            }
        }

        if (guidanceText == null)
        {
            guidanceText = FindText("Guidance");
        }

        if (statusText == null)
        {
            statusText = FindText("StatusText");
        }

        if (originalIdeaText == null)
        {
            originalIdeaText = FindText("OriginalIdeaText");
        }

        if (runtimeStatusText == null)
        {
            runtimeStatusText = FindText("ExpansionStatusText");
        }

        if (optionsParent == null)
        {
            GameObject optionsObject = GameObject.Find("ExpansionOptionsPanel");

            if (optionsObject != null)
            {
                optionsParent = optionsObject.transform;
            }
        }

        ResolveOptionReferences();
        SetTitleText();
    }

    private void ResolveOptionReferences()
    {
        for (int i = 0; i < 3; i++)
        {
            GameObject optionObject = GameObject.Find("ExpansionOption" + (i + 1));

            if (optionObject == null)
            {
                continue;
            }

            if (optionButtons[i] == null)
            {
                optionButtons[i] = optionObject.GetComponent<Button>();
            }

            if (optionCardImages[i] == null)
            {
                optionCardImages[i] = optionObject.GetComponent<Image>();
            }

            if (optionTitleTexts[i] == null)
            {
                Transform titleTransform = optionObject.transform.Find("TitleText");

                if (titleTransform != null)
                {
                    optionTitleTexts[i] = titleTransform.GetComponent<Text>();
                }
            }

            if (optionDescriptionTexts[i] == null)
            {
                Transform descriptionTransform = optionObject.transform.Find("DescriptionText");

                if (descriptionTransform != null)
                {
                    optionDescriptionTexts[i] = descriptionTransform.GetComponent<Text>();
                }
            }
        }
    }

    private Text FindText(string objectName)
    {
        GameObject textObject = GameObject.Find(objectName);
        return textObject != null ? textObject.GetComponent<Text>() : null;
    }

    private void SetTitleText()
    {
        Text titleText = FindText("SurveyTitleText");

        if (titleText != null)
        {
            titleText.text = "Choose a Direction";
        }
    }

    private void WireButtons()
    {
        if (submitButton != null)
        {
            submitButton.onClick.RemoveAllListeners();
            submitButton.onClick.AddListener(ContinueToLevel);
            SetButtonLabel(submitButton, "Generate Level");
        }

        for (int i = 0; i < optionButtons.Length; i++)
        {
            int optionIndex = i;

            if (optionButtons[i] != null)
            {
                optionButtons[i].onClick.RemoveAllListeners();
                optionButtons[i].onClick.AddListener(() => SelectOption(optionIndex));
            }
        }
    }

    private void LoadOriginalIdea()
    {
        originalIdea = !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaText)
            ? CreativeWorkshopContext.IdeaText
            : PlayerPrefs.GetString(CreativeWorkshopContext.IdeaTextPrefsKey, "");
        originalIdea = CleanText(originalIdea);
    }

    private void RefreshStaticText()
    {
        if (guidanceText != null)
        {
            guidanceText.text = "The LLM will turn your rough idea into three playable directions. Choose one to guide the generated level.";
        }

        if (originalIdeaText != null)
        {
            originalIdeaText.text = string.IsNullOrEmpty(originalIdea)
                ? "Original idea: missing"
                : "Original idea: " + originalIdea;
        }

        SetStatus("");
    }

    private IEnumerator RequestExpansionRoutine()
    {
        if (isRequesting)
        {
            yield break;
        }

        selectedOptionIndex = -1;
        options = new CreativeIdeaExpansionOption[0];
        SetOptionTexts(null);

        if (string.IsNullOrEmpty(originalIdea))
        {
            SetStatus("No creative idea was found. Please return to the creative workshop.");
            UpdateSubmitState();
            yield break;
        }

        isRequesting = true;
        SetStatus("Generating expanded directions...");
        UpdateSubmitState();

        CreativeIdeaExpansionRequest requestBody = new CreativeIdeaExpansionRequest
        {
            ideaId = GetIdeaId(),
            sessionId = GetSessionId(),
            ideaText = originalIdea,
            sceneName = SceneManager.GetActiveScene().name
        };

        string json = JsonUtility.ToJson(requestBody);
        byte[] body = Encoding.UTF8.GetBytes(json);

        using (UnityWebRequest request = new UnityWebRequest(GetBackendUrl(ExpansionPath), "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(body);
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.SetRequestHeader("Accept", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);

            yield return request.SendWebRequest();

            if (request.result == UnityWebRequest.Result.Success)
            {
                ApplyResponse(request.downloadHandler.text);
            }
            else
            {
                Debug.LogWarning(
                    "CreativeIdeaExpansionController request failed:"
                    + " error=" + request.error
                    + ", responseCode=" + request.responseCode
                );
                ApplyOptions(CreateLocalFallbackOptions(), true, "Using local backup directions.");
            }
        }

        isRequesting = false;
        UpdateSubmitState();
    }

    private void ApplyResponse(string json)
    {
        CreativeIdeaExpansionResponse response = null;

        try
        {
            response = JsonUtility.FromJson<CreativeIdeaExpansionResponse>(json);
        }
        catch (Exception exception)
        {
            Debug.LogWarning("CreativeIdeaExpansionController could not parse expansion JSON: " + exception.Message);
        }

        if (response == null || response.options == null || response.options.Length < 3)
        {
            ApplyOptions(CreateLocalFallbackOptions(), true, "Using local backup directions.");
            return;
        }

        ApplyOptions(
            response.options,
            response.usedFallback,
            response.usedFallback ? "Using backup directions." : "Choose one direction."
        );
    }

    private void ApplyOptions(CreativeIdeaExpansionOption[] newOptions, bool usedFallback, string statusMessage)
    {
        options = NormalizeOptions(newOptions);
        selectedOptionIndex = -1;
        SetOptionTexts(options);
        SetStatus(statusMessage);

        if (logExpansionEvents)
        {
            Debug.Log(
                "Creative idea expansion options ready:"
                + " count=" + options.Length
                + ", usedFallback=" + usedFallback
            );
        }
    }

    private CreativeIdeaExpansionOption[] NormalizeOptions(CreativeIdeaExpansionOption[] source)
    {
        CreativeIdeaExpansionOption[] normalized = new CreativeIdeaExpansionOption[3];
        CreativeIdeaExpansionOption[] fallback = CreateLocalFallbackOptions();

        for (int i = 0; i < 3; i++)
        {
            CreativeIdeaExpansionOption option = source != null && i < source.Length ? source[i] : null;

            if (option == null)
            {
                normalized[i] = fallback[i];
                continue;
            }

            normalized[i] = new CreativeIdeaExpansionOption
            {
                id = string.IsNullOrEmpty(option.id) ? fallback[i].id : option.id,
                title = string.IsNullOrEmpty(option.title) ? fallback[i].title : option.title,
                description = string.IsNullOrEmpty(option.description) ? fallback[i].description : option.description,
                promptText = string.IsNullOrEmpty(option.promptText) ? option.description : option.promptText
            };
        }

        return normalized;
    }

    private void SetOptionTexts(CreativeIdeaExpansionOption[] displayOptions)
    {
        for (int i = 0; i < 3; i++)
        {
            bool hasOption = displayOptions != null && i < displayOptions.Length && displayOptions[i] != null;

            if (optionTitleTexts[i] != null)
            {
                optionTitleTexts[i].text = hasOption ? displayOptions[i].title : "Option " + (i + 1);
            }

            if (optionDescriptionTexts[i] != null)
            {
                optionDescriptionTexts[i].text = hasOption ? displayOptions[i].description : "";
            }
        }

        UpdateOptionVisuals();
    }

    private void SelectOption(int index)
    {
        if (isRequesting || options == null || index < 0 || index >= options.Length)
        {
            return;
        }

        selectedOptionIndex = index;
        SetStatus("Selected: " + options[index].title);
        UpdateOptionVisuals();
        UpdateSubmitState();
    }

    private void UpdateOptionVisuals()
    {
        bool hasOptions = options != null && options.Length >= 3;

        for (int i = 0; i < 3; i++)
        {
            if (optionButtons[i] != null)
            {
                optionButtons[i].interactable = !isRequesting && !isContinuing && hasOptions;
            }

            if (optionCardImages[i] != null)
            {
                if (!hasOptions || isRequesting || isContinuing)
                {
                    optionCardImages[i].color = disabledCardColor;
                }
                else
                {
                    optionCardImages[i].color = i == selectedOptionIndex ? selectedCardColor : normalCardColor;
                }
            }
        }
    }

    private void UpdateSubmitState()
    {
        if (submitButton != null)
        {
            submitButton.interactable = !isRequesting
                && !isContinuing
                && options != null
                && selectedOptionIndex >= 0
                && selectedOptionIndex < options.Length;
        }

        UpdateOptionVisuals();
    }

    private void ContinueToLevel()
    {
        if (isContinuing || selectedOptionIndex < 0 || options == null || selectedOptionIndex >= options.Length)
        {
            return;
        }

        StartCoroutine(RecordChoiceAndContinueRoutine());
    }

    private IEnumerator RecordChoiceAndContinueRoutine()
    {
        isContinuing = true;
        UpdateSubmitState();
        SetStatus("Recording selected direction...");

        CreativeIdeaExpansionOption selectedOption = options[selectedOptionIndex];
        string finalIdeaText = BuildFinalIdeaText(options[selectedOptionIndex]);
        string ideaId = GetIdeaId();
        string sessionId = GetSessionId();

        CreativeWorkshopContext.SetIdea(ideaId, sessionId, finalIdeaText);
        LevelStudyRecorder.UpdateCustomRoundIdea(ideaId, finalIdeaText);

        CreativeIdeaExpansionChoiceRecord record = new CreativeIdeaExpansionChoiceRecord
        {
            eventType = "creative-expansion-choice",
            ideaId = ideaId,
            sessionId = sessionId,
            gameRoundId = LevelStudyRecorder.CurrentGameRoundId,
            gameRoundIndex = LevelStudyRecorder.CurrentGameRoundIndex,
            originalIdeaText = originalIdea,
            selectedOptionId = selectedOption.id,
            selectedOptionTitle = selectedOption.title,
            selectedOptionDescription = selectedOption.description,
            selectedOptionPromptText = selectedOption.promptText,
            finalIdeaText = finalIdeaText,
            sceneName = SceneManager.GetActiveScene().name,
            officialRound = LevelStudyRecorder.IsOfficialRoundFlow,
            timestamp = DateTime.UtcNow.ToString("o")
        };

        yield return PostChoiceRecordRoutine(record);

        if (logExpansionEvents)
        {
            Debug.Log(
                "Creative idea expansion selected:"
                + " option=" + selectedOption.id
                + ", ideaId=" + ideaId
            );
        }

        if (!string.IsNullOrEmpty(nextSceneName))
        {
            SceneManager.LoadScene(nextSceneName);
        }
    }

    private IEnumerator PostChoiceRecordRoutine(CreativeIdeaExpansionChoiceRecord record)
    {
        string json = JsonUtility.ToJson(record);
        byte[] body = Encoding.UTF8.GetBytes(json);

        using (UnityWebRequest request = new UnityWebRequest(GetBackendUrl(ExpansionChoicePath), "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(body);
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.SetRequestHeader("Accept", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);

            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning(
                    "CreativeIdeaExpansionController choice record failed:"
                    + " error=" + request.error
                    + ", responseCode=" + request.responseCode
                );
            }
        }
    }

    private string BuildFinalIdeaText(CreativeIdeaExpansionOption option)
    {
        string selectedText = CleanText(option.promptText);

        if (string.IsNullOrEmpty(selectedText))
        {
            selectedText = CleanText(option.description);
        }

        return "Selected design direction: "
            + CleanText(option.title)
            + ". "
            + selectedText
            + " Original player idea to respect: \""
            + originalIdea
            + "\". Use the selected design direction as the primary generation intent, but do not drift away from the original idea.";
    }

    private CreativeIdeaExpansionOption[] CreateLocalFallbackOptions()
    {
        if (ContainsCjk(originalIdea))
        {
            return new[]
            {
                new CreativeIdeaExpansionOption
                {
                    id = "A",
                    title = "狭窄绕路型",
                    description = "围绕原始想法加入更明确的瓶颈通道和绕路压力，适合做成需要提前规划的两箱关卡。",
                    promptText = "以狭窄通道、绕路规划和轻度水域阻隔为主，保持原始想法的主题，不加入生成器不支持的机制。"
                },
                new CreativeIdeaExpansionOption
                {
                    id = "B",
                    title = "分离目标型",
                    description = "把两个目标区域适度分开，让玩家需要决定先处理哪只箱子，同时保留原始创意的核心感觉。",
                    promptText = "以分离目标、路线选择和箱子顺序规划为主，参考原始想法并避免偏离其主题。"
                },
                new CreativeIdeaExpansionOption
                {
                    id = "C",
                    title = "紧凑精确型",
                    description = "地图更紧凑，移动空间更受限，强调少量关键推动和防卡死判断。",
                    promptText = "以紧凑空间、关键推动和死锁规避为主，原始想法作为主题约束和风格来源。"
                }
            };
        }

        return new[]
        {
            new CreativeIdeaExpansionOption
            {
                id = "A",
                title = "Narrow Detour",
                description = "Turn the idea into a route-planning puzzle with a clear choke point, a small detour, and light water pressure.",
                promptText = "Use narrow routes, detour planning, and light water obstacles while preserving the original idea's theme."
            },
            new CreativeIdeaExpansionOption
            {
                id = "B",
                title = "Split Goals",
                description = "Separate the two target areas so the player has to choose box order while still following the original idea.",
                promptText = "Use split goals, route choice, and box-order planning while keeping the original idea as the main constraint."
            },
            new CreativeIdeaExpansionOption
            {
                id = "C",
                title = "Compact Precision",
                description = "Make the map tighter and more exact, with a few important pushes and careful deadlock avoidance.",
                promptText = "Use compact space, precise pushes, and deadlock avoidance while treating the original idea as the theme."
            }
        };
    }

    private string GetBackendUrl(string path)
    {
        string baseUrl = string.IsNullOrEmpty(backendBaseUrl)
            ? DefaultBackendBaseUrl
            : backendBaseUrl.TrimEnd('/');

        return baseUrl + path;
    }

    private string GetIdeaId()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaId)
            ? CreativeWorkshopContext.IdeaId
            : PlayerPrefs.GetString(CreativeWorkshopContext.IdeaIdPrefsKey, "");
    }

    private string GetSessionId()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.SessionId)
            ? CreativeWorkshopContext.SessionId
            : PlayerPrefs.GetString(CreativeWorkshopContext.SessionIdPrefsKey, "");
    }

    private void SetStatus(string message)
    {
        if (runtimeStatusText != null)
        {
            runtimeStatusText.text = message;
        }

        if (statusText != null)
        {
            statusText.text = message;
        }
    }

    private void SetButtonLabel(Button button, string label)
    {
        if (button == null)
        {
            return;
        }

        TMP_Text tmpText = button.GetComponentInChildren<TMP_Text>();

        if (tmpText != null)
        {
            tmpText.text = label;
            return;
        }

        Text text = button.GetComponentInChildren<Text>();

        if (text != null)
        {
            text.text = label;
        }
    }

    private string CleanText(string value)
    {
        return string.IsNullOrEmpty(value)
            ? ""
            : string.Join(" ", value.Trim().Split((char[])null, StringSplitOptions.RemoveEmptyEntries));
    }

    private bool ContainsCjk(string text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return false;
        }

        for (int i = 0; i < text.Length; i++)
        {
            if (text[i] >= '\u4e00' && text[i] <= '\u9fff')
            {
                return true;
            }
        }

        return false;
    }

    [Serializable]
    public class CreativeIdeaExpansionRequest
    {
        public string ideaId;
        public string sessionId;
        public string ideaText;
        public string sceneName;
    }

    [Serializable]
    public class CreativeIdeaExpansionResponse
    {
        public CreativeIdeaExpansionOption[] options;
        public bool usedFallback;
        public string message;
    }

    [Serializable]
    public class CreativeIdeaExpansionOption
    {
        public string id;
        public string title;
        public string description;
        public string promptText;
    }

    [Serializable]
    public class CreativeIdeaExpansionChoiceRecord
    {
        public string eventType;
        public string ideaId;
        public string sessionId;
        public string gameRoundId;
        public int gameRoundIndex;
        public string originalIdeaText;
        public string selectedOptionId;
        public string selectedOptionTitle;
        public string selectedOptionDescription;
        public string selectedOptionPromptText;
        public string finalIdeaText;
        public string sceneName;
        public bool officialRound;
        public string timestamp;
    }
}
