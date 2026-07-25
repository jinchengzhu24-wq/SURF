using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class CreativeWorkshopInputController : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "http://111.231.136.4:8000";
    private const string CreativeIdeaPath = "/record-creative-idea";

    [Header("Scene UI")]
    public InputField ideaInput;
    public Button submitButton;
    public Text statusText;

    [Header("Input")]
    public string ideaPlaceholder = "Enter your idea";
    [Min(1)]
    public int ideaCharacterLimit = 240;

    [Header("Flow")]
    public string nextSceneName = "Questionnaire";
    public float nextSceneDelaySeconds = 0.75f;

    [Header("Backend")]
    public string backendBaseUrl = DefaultBackendBaseUrl;
    public int requestTimeoutSeconds = 5;
    public bool continueWhenBackendFails = true;
    public bool logIdeaEvents = true;

    private float startedAt;
    private bool isSubmitting;

    private void Awake()
    {
        startedAt = Time.realtimeSinceStartup;
    }

    private void Start()
    {
        ResolveSceneReferences();
        ConfigureInput();
        WireButtons();
        SetStatus("");
        UpdateSubmitState();
    }

    private void OnDestroy()
    {
        if (ideaInput != null)
        {
            ideaInput.onValueChanged.RemoveListener(OnIdeaChanged);
        }
    }

    private void ResolveSceneReferences()
    {
        if (ideaInput == null)
        {
            GameObject inputObject = GameObject.Find("InputField");

            if (inputObject != null)
            {
                ideaInput = inputObject.GetComponent<InputField>();
            }
        }

        if (ideaInput == null)
        {
            ideaInput = FindObjectOfType<InputField>();
        }

        if (submitButton == null)
        {
            GameObject submitObject = GameObject.Find("SubmitButton");

            if (submitObject != null)
            {
                submitButton = submitObject.GetComponent<Button>();
            }
        }
    }

    private void ConfigureInput()
    {
        if (ideaInput == null)
        {
            return;
        }

        ideaInput.contentType = InputField.ContentType.Standard;
        ideaInput.lineType = InputField.LineType.SingleLine;
        ideaInput.characterLimit = Mathf.Max(1, ideaCharacterLimit);

        if (ideaInput.textComponent != null)
        {
            ideaInput.textComponent.color = Color.black;
        }

        Text placeholderText = ideaInput.placeholder as Text;

        if (placeholderText != null && !string.IsNullOrEmpty(ideaPlaceholder))
        {
            placeholderText.text = ideaPlaceholder;
            placeholderText.color = new Color(0f, 0f, 0f, 0.45f);
        }
    }

    private void WireButtons()
    {
        if (submitButton != null)
        {
            submitButton.onClick.RemoveAllListeners();
            submitButton.onClick.AddListener(Submit);
        }

        if (ideaInput != null)
        {
            ideaInput.onValueChanged.RemoveListener(OnIdeaChanged);
            ideaInput.onValueChanged.AddListener(OnIdeaChanged);
        }
    }

    private void OnIdeaChanged(string value)
    {
        SetStatus("");
        UpdateSubmitState();
    }

    private void UpdateSubmitState()
    {
        if (submitButton != null)
        {
            submitButton.interactable = !isSubmitting && !string.IsNullOrEmpty(IdeaText);
        }
    }

    private void Submit()
    {
        if (isSubmitting || string.IsNullOrEmpty(IdeaText))
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

        CreativeIdeaRecord record = CreateIdeaRecord();
        string json = JsonUtility.ToJson(record);
        byte[] body = Encoding.UTF8.GetBytes(json);

        using (UnityWebRequest request = new UnityWebRequest(GetBackendUrl(CreativeIdeaPath), "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(body);
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);

            yield return request.SendWebRequest();

            if (request.result == UnityWebRequest.Result.Success)
            {
                yield return AcceptIdeaAndContinue(record, "Submitted.", true);
            }
            else
            {
                Debug.LogWarning(
                    "Creative workshop idea submit failed:"
                    + " error=" + request.error
                    + ", responseCode=" + request.responseCode
                );

                if (continueWhenBackendFails)
                {
                    yield return AcceptIdeaAndContinue(record, "Saved locally. Continuing...", false);
                }
                else
                {
                    SetStatus("Submit failed: " + request.error);
                }
            }
        }

        isSubmitting = false;
        UpdateSubmitState();
    }

    private IEnumerator AcceptIdeaAndContinue(CreativeIdeaRecord record, string statusMessage, bool recordedRemotely)
    {
        SetStatus(statusMessage);
        CreativeWorkshopContext.BeginIdea(record.ideaId, record.sessionId, record.ideaText);
        LevelStudyRecorder.BeginCustomRound(record.ideaId, record.ideaText);

        if (logIdeaEvents)
        {
            Debug.Log(
                "Creative workshop idea "
                + (recordedRemotely ? "submitted" : "saved locally after backend failure")
                + ": ideaId=" + record.ideaId
            );
        }

        if (!string.IsNullOrEmpty(nextSceneName))
        {
            yield return new WaitForSeconds(Mathf.Max(0f, nextSceneDelaySeconds));
            SceneManager.LoadScene(nextSceneName);
        }
    }

    private CreativeIdeaRecord CreateIdeaRecord()
    {
        return new CreativeIdeaRecord
        {
            eventType = "creative-idea",
            ideaId = Guid.NewGuid().ToString("N"),
            sessionId = GetOrCreateSessionId(),
            ideaText = IdeaText,
            sceneName = SceneManager.GetActiveScene().name,
            officialRound = LevelStudyRecorder.IsOfficialRoundFlow,
            timestamp = DateTime.UtcNow.ToString("o"),
            durationSeconds = Mathf.Round((Time.realtimeSinceStartup - startedAt) * 100f) / 100f
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

    private void SetStatus(string message)
    {
        if (statusText != null)
        {
            statusText.text = message;
        }
    }

    private string IdeaText
    {
        get
        {
            return ideaInput != null && ideaInput.text != null
                ? ideaInput.text.Trim()
                : "";
        }
    }
}

[Serializable]
public class CreativeIdeaRecord
{
    public string eventType;
    public string ideaId;
    public string sessionId;
    public string ideaText;
    public string sceneName;
    public bool officialRound;
    public string timestamp;
    public float durationSeconds;
}
