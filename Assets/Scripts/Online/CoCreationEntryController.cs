using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public sealed class CoCreationEntryController : MonoBehaviour
{
    private const string DefaultCoCreationUrl = "http://111.231.136.4:8010/";
    private static readonly Color ReadyStatusColor = new Color(0.36f, 0.36f, 0.36f, 1f);
    private static readonly Color WaitingStatusColor = new Color(0.60f, 0.40f, 0f, 1f);
    private static readonly Color ErrorStatusColor = new Color(0.71f, 0.14f, 0.09f, 1f);

    [SerializeField] private string coCreationUrl = DefaultCoCreationUrl;
    [SerializeField] private Button openLabButton;
    [SerializeField] private Text statusText;
    [SerializeField] private int requestTimeoutSeconds = 15;

    private string launchUrl = "";
    private bool creatingSession;
    private Coroutine trackingRoutine;

    private void Awake()
    {
        if (openLabButton != null)
        {
            openLabButton.onClick.RemoveListener(OpenCoCreationLab);
            openLabButton.onClick.AddListener(OpenCoCreationLab);
            openLabButton.interactable = false;
        }
        else
        {
            Debug.LogWarning("CoCreationEntryController: Open lab button is missing.");
        }
    }

    private void Start()
    {
        if (!TryGetCoCreationUrl(out _))
        {
            ApplyFailure("The co-creation lab URL is missing or invalid.");
            return;
        }

        if (!CoCreationDraftContext.HasDraft)
        {
            ApplyFailure("No verified Unity draft is available for Stage 1.");
            return;
        }

        StartCoroutine(CreateSession());
    }

    private void OnDestroy()
    {
        if (openLabButton != null)
        {
            openLabButton.onClick.RemoveListener(OpenCoCreationLab);
        }

        if (trackingRoutine != null)
        {
            StopCoroutine(trackingRoutine);
        }
    }

    public void OpenCoCreationLab()
    {
        if (creatingSession)
        {
            return;
        }

        if (string.IsNullOrWhiteSpace(launchUrl))
        {
            if (CoCreationDraftContext.HasDraft)
            {
                StartCoroutine(CreateSession());
            }
            else
            {
                ApplyFailure("No verified Unity draft is available for Stage 1.");
            }
            return;
        }

        Application.OpenURL(launchUrl);
        SetStatus(
            "Co-creation lab opened. Unity is waiting for final confirmation and intention.",
            ReadyStatusColor
        );
    }

    private IEnumerator CreateSession()
    {
        if (creatingSession || !TryGetCoCreationUrl(out string baseUrl))
        {
            yield break;
        }

        creatingSession = true;
        launchUrl = "";
        SetButtonState(false, "CREATING SESSION...");
        SetStatus(
            "Uploading the verified first draft as Stage 1...",
            WaitingStatusColor
        );

        CreateCoCreationSessionRequest payload = new CreateCoCreationSessionRequest
        {
            rows = CoCreationDraftContext.Rows,
            initialDraftMethod = CoCreationDraftContext.InitialDraftMethod,
            language = Application.systemLanguage == SystemLanguage.ChineseSimplified
                || Application.systemLanguage == SystemLanguage.ChineseTraditional
                ? "zh-CN"
                : "en",
            idempotencyKey = CoCreationDraftContext.CreationKey,
            matchId = OnlineMatchContext.HasMatch ? OnlineMatchContext.MatchId : null,
            playerNumber = OnlineMatchContext.HasMatch ? OnlineMatchContext.PlayerNumber : 0
        };
        string endpoint = baseUrl.TrimEnd('/') + "/api/sessions";

        using (UnityWebRequest request = new UnityWebRequest(endpoint, "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(
                Encoding.UTF8.GetBytes(JsonUtility.ToJson(payload))
            );
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            yield return request.SendWebRequest();

            creatingSession = false;

            if (request.result != UnityWebRequest.Result.Success)
            {
                ApplyFailure("Stage 1 upload failed. Select the button to retry.");
                Debug.LogWarning(
                    "CoCreationEntryController: Session creation failed: "
                    + request.error + " response=" + request.downloadHandler.text
                );
                yield break;
            }

            CreateCoCreationSessionResponse response;

            try
            {
                response = JsonUtility.FromJson<CreateCoCreationSessionResponse>(
                    request.downloadHandler.text
                );
            }
            catch (Exception exception)
            {
                ApplyFailure("The co-creation server returned an invalid response. Select the button to retry.");
                Debug.LogWarning(
                    "CoCreationEntryController: Invalid session response. " + exception.Message
                );
                yield break;
            }

            if (response == null
                || string.IsNullOrWhiteSpace(response.sessionId)
                || !TryValidateLaunchUrl(response.launchUrl, out launchUrl))
            {
                ApplyFailure("The co-creation server returned an incomplete response. Select the button to retry.");
                yield break;
            }

            CoCreationDraftContext.RecordSession(
                response.sessionId,
                response.integrationToken
            );
            SetButtonState(true, "OPEN CO-CREATION LAB");
            SetStatus(
                "Stage 1 is synchronized. Open the lab to begin co-creation.",
                ReadyStatusColor
            );

            if (trackingRoutine != null)
            {
                StopCoroutine(trackingRoutine);
            }
            trackingRoutine = StartCoroutine(TrackSessionCompletion());
        }
    }

    private IEnumerator TrackSessionCompletion()
    {
        while (CoCreationDraftContext.HasDraft
            && !string.IsNullOrWhiteSpace(CoCreationDraftContext.SessionId)
            && !string.IsNullOrWhiteSpace(CoCreationDraftContext.IntegrationToken))
        {
            string endpoint = coCreationUrl.TrimEnd('/')
                + "/api/integrations/sessions/"
                + UnityWebRequest.EscapeURL(CoCreationDraftContext.SessionId);

            using (UnityWebRequest request = UnityWebRequest.Get(endpoint))
            {
                request.SetRequestHeader(
                    "Authorization",
                    "Bearer " + CoCreationDraftContext.IntegrationToken
                );
                request.timeout = Mathf.Max(1, requestTimeoutSeconds);
                yield return request.SendWebRequest();

                if (request.result == UnityWebRequest.Result.Success)
                {
                    CoCreationIntegrationResponse response = null;

                    try
                    {
                        response = JsonUtility.FromJson<CoCreationIntegrationResponse>(
                            request.downloadHandler.text
                        );
                    }
                    catch (Exception exception)
                    {
                        Debug.LogWarning(
                            "CoCreationEntryController: Invalid tracking response. "
                            + exception.Message
                        );
                    }

                    if (response != null && response.status == "awaiting_intention")
                    {
                        SetStatus(
                            "Final Stage confirmed. Complete the design intention in the lab.",
                            WaitingStatusColor
                        );
                    }
                    else if (response != null
                        && response.status == "completed"
                        && response.finalRows != null
                        && response.finalRows.Length == 10)
                    {
                        HandleCompletedSession(response.finalRows);
                        yield break;
                    }
                }
            }

            yield return new WaitForSecondsRealtime(1f);
        }
    }

    private void HandleCompletedSession(string[] finalRows)
    {
        SetStatus(
            "Co-creation session complete. The confirmed Stage is synchronized.",
            ReadyStatusColor
        );
        SetButtonState(false, "SESSION COMPLETE");

        if (!OnlineMatchContext.HasMatch)
        {
            return;
        }

        const string waitingSceneName = "Challenge_Waiting";
        if (!Application.CanStreamedLevelBeLoaded(waitingSceneName))
        {
            ApplyFailure("Challenge_Waiting is not available in Build Settings.");
            return;
        }

        OnlineMatchContext.StageChallenge(
            finalRows,
            CoCreationDraftContext.InitialDraftMethod
        );
        CoCreationDraftContext.Clear();
        SceneManager.LoadScene(waitingSceneName);
    }

    private bool TryGetCoCreationUrl(out string targetUrl)
    {
        return TryValidateLaunchUrl(coCreationUrl, out targetUrl);
    }

    private static bool TryValidateLaunchUrl(string value, out string targetUrl)
    {
        targetUrl = "";
        string candidate = value == null ? "" : value.Trim();

        if (!Uri.TryCreate(candidate, UriKind.Absolute, out Uri uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            return false;
        }

        targetUrl = uri.AbsoluteUri;
        return true;
    }

    private void ApplyFailure(string message)
    {
        creatingSession = false;
        launchUrl = "";
        SetButtonState(CoCreationDraftContext.HasDraft, "RETRY SESSION");
        SetStatus(message, ErrorStatusColor);
    }

    private void SetButtonState(bool interactable, string label)
    {
        if (openLabButton == null)
        {
            return;
        }

        openLabButton.interactable = interactable;
        Text buttonText = openLabButton.GetComponentInChildren<Text>(true);
        if (buttonText != null)
        {
            buttonText.text = label;
        }
    }

    private void SetStatus(string message, Color color)
    {
        if (statusText != null)
        {
            statusText.text = message;
            statusText.color = color;
        }
    }
}

[Serializable]
public sealed class CreateCoCreationSessionRequest
{
    public string[] rows;
    public string initialDraftMethod;
    public string language;
    public string idempotencyKey;
    public string matchId;
    public int playerNumber;
}

[Serializable]
public sealed class CreateCoCreationSessionResponse
{
    public string sessionId;
    public string launchUrl;
    public string integrationToken;
}

[Serializable]
public sealed class CoCreationIntegrationResponse
{
    public string sessionId;
    public string status;
    public string finalVersionId;
    public string[] finalRows;
}
