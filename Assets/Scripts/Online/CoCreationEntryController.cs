using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

#if UNITY_WEBGL && !UNITY_EDITOR
using System.Runtime.InteropServices;
#endif

public sealed class CoCreationEntryController : MonoBehaviour
{
    private const string DefaultCoCreationUrl = "http://111.231.136.4/cocreation/";
    private static readonly Color ReadyStatusColor = new Color(0.36f, 0.36f, 0.36f, 1f);
    private static readonly Color WaitingStatusColor = new Color(0.60f, 0.40f, 0f, 1f);
    private static readonly Color ErrorStatusColor = new Color(0.71f, 0.14f, 0.09f, 1f);

    [SerializeField] private string coCreationUrl = DefaultCoCreationUrl;
    [SerializeField] private Button openLabButton;
    [SerializeField] private Text statusText;
    [SerializeField] private int requestTimeoutSeconds = 15;

    private string launchUrl = "";
    private bool creatingSession;
    private bool processingRegeneration;
    private Coroutine trackingRoutine;

#if UNITY_WEBGL && !UNITY_EDITOR
    [DllImport("__Internal")]
    private static extern void SokobanSetDraftRegenerationBridgeSession(
        string sessionId,
        int protocolVersion
    );

    [DllImport("__Internal")]
    private static extern void SokobanReturnDraftRegeneration(
        string sessionId,
        string requestId,
        string status
    );

    [DllImport("__Internal")]
    private static extern void SokobanShowCoCreationLab(
        string url,
        string sessionId
    );
#endif

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

#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanSetDraftRegenerationBridgeSession(
            CoCreationDraftContext.SessionId ?? "",
            3
        );
#endif
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

        if (CoCreationDraftContext.HasRegenerationResult)
        {
            if (!TryBuildResumeLabUrl(CoCreationDraftContext.SessionId, out launchUrl))
            {
                ApplyFailure("The existing co-creation session could not be resumed.");
                return;
            }
            SetButtonState(false, "SYNCING NEW DRAFT...");
            SetStatus("Returning the regenerated draft to the co-creation lab...", WaitingStatusColor);
            StartCoroutine(SubmitRegenerationResult());
            return;
        }

        if (CoCreationPlayContext.ConsumeEmbeddedReturnPending())
        {
            if (!CoCreationDraftContext.HasSession
                || !TryBuildResumeLabUrl(
                    CoCreationDraftContext.SessionId,
                    out launchUrl))
            {
                ApplyFailure(
                    "The existing co-creation session could not be resumed."
                );
                return;
            }

            SetButtonState(true, "RETURN TO CO-CREATION LAB");
            SetStatus(
                "Stage play complete. Continue designing in the existing lab tab.",
                ReadyStatusColor
            );
            trackingRoutine = StartCoroutine(TrackSessionCompletion());
            return;
        }

        if (CoCreationDraftContext.HasSession)
        {
            if (!TryBuildResumeLabUrl(CoCreationDraftContext.SessionId, out launchUrl))
            {
                ApplyFailure("The existing co-creation session could not be resumed.");
                return;
            }
            SetButtonState(true, "RETURN TO CO-CREATION LAB");
            SetStatus("Draft preview is synchronized. Continue in the existing lab tab.", ReadyStatusColor);
            trackingRoutine = StartCoroutine(TrackSessionCompletion());
            return;
        }

        StartCoroutine(CreateSession());
    }

    private void OnDestroy()
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanSetDraftRegenerationBridgeSession("", 0);
#endif
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

        if (CoCreationDraftContext.HasRegenerationResult && !processingRegeneration)
        {
            StartCoroutine(SubmitRegenerationResult());
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

#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanShowCoCreationLab(
            launchUrl,
            CoCreationDraftContext.SessionId ?? ""
        );
#else
        Application.OpenURL(launchUrl);
#endif
        SetStatus(
            "Co-creation lab opened. Unity is waiting for final confirmation and intention.",
            ReadyStatusColor
        );
    }

    public void ReceiveBrowserDraftRegenerationRequest(string payloadJson)
    {
        DraftRegenerationBridgeRequest payload = null;
        try
        {
            payload = JsonUtility.FromJson<DraftRegenerationBridgeRequest>(payloadJson);
        }
        catch (Exception exception)
        {
            Debug.LogWarning("CoCreationEntryController: Invalid regeneration bridge payload. " + exception.Message);
        }

        if (payload == null
            || payload.sessionId != CoCreationDraftContext.SessionId
            || string.IsNullOrWhiteSpace(payload.requestId)
            || processingRegeneration
            || CoCreationDraftContext.IsRegenerating)
        {
            return;
        }

        StartCoroutine(ClaimAndStartRegeneration(payload.requestId));
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
            "Uploading the verified first draft for preview...",
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
                    + " endpoint=" + endpoint
                    + " result=" + request.result
                    + " status=" + request.responseCode
                    + " error=" + request.error
                    + " response=" + request.downloadHandler.text
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
#if UNITY_WEBGL && !UNITY_EDITOR
            SokobanSetDraftRegenerationBridgeSession(
                CoCreationDraftContext.SessionId,
                3
            );
#endif
            SetButtonState(true, "OPEN CO-CREATION LAB");
            SetStatus(
                "Draft preview is synchronized. Open the lab to review it.",
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
                        && response.draftRegeneration != null
                        && (response.draftRegeneration.status == "pending"
                            || response.draftRegeneration.status == "claimed")
                        && !processingRegeneration
                        && !CoCreationDraftContext.IsRegenerating)
                    {
                        StartCoroutine(ClaimAndStartRegeneration(
                            response.draftRegeneration.requestId
                        ));
                        yield break;
                    }
                    else if (response != null
                        && response.status == "completed"
                        && response.finalRows != null
                        && response.finalRows.Length == 10)
                    {
                        HandleCompletedSession(
                            response.finalRows,
                            response.designerIntention
                        );
                        yield break;
                    }
                }
            }

            yield return new WaitForSecondsRealtime(1f);
        }
    }

    private IEnumerator ClaimAndStartRegeneration(string requestId)
    {
        if (processingRegeneration || !CoCreationDraftContext.HasSession)
        {
            yield break;
        }

        processingRegeneration = true;
        SetButtonState(false, "REGENERATING DRAFT...");
        SetStatus("Claiming the persistent draft regeneration request...", WaitingStatusColor);
        string endpoint = coCreationUrl.TrimEnd('/')
            + "/api/integrations/sessions/"
            + UnityWebRequest.EscapeURL(CoCreationDraftContext.SessionId)
            + "/draft-regenerations/"
            + UnityWebRequest.EscapeURL(requestId)
            + "/claim";

        using (UnityWebRequest request = new UnityWebRequest(endpoint, "POST"))
        {
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Authorization", "Bearer " + CoCreationDraftContext.IntegrationToken);
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
            {
                processingRegeneration = false;
                TryBuildResumeLabUrl(CoCreationDraftContext.SessionId, out launchUrl);
                SetButtonState(true, "RETURN TO CO-CREATION LAB");
                SetStatus(
                    "The regeneration request could not be claimed. Return to the lab; the previous draft is preserved.",
                    ErrorStatusColor
                );
                NotifyBrowserRegenerationReturn(requestId, "failed");
                trackingRoutine = StartCoroutine(TrackSessionCompletion());
                yield break;
            }
        }

        CoCreationDraftContext.BeginRegeneration(requestId);
        if (!CoCreationDraftContext.HasSavedLevelDesignPlan)
        {
            CoCreationDraftContext.FailRegeneration("blueprint_unavailable");
            processingRegeneration = false;
            StartCoroutine(SubmitRegenerationResult());
            yield break;
        }
        const string generationScene = "DG_Level";
        if (!Application.CanStreamedLevelBeLoaded(generationScene))
        {
            CoCreationDraftContext.FailRegeneration("generation_failed");
            processingRegeneration = false;
            StartCoroutine(SubmitRegenerationResult());
            yield break;
        }
        SceneManager.LoadScene(generationScene);
    }

    private IEnumerator SubmitRegenerationResult()
    {
        if (processingRegeneration || !CoCreationDraftContext.HasRegenerationResult)
        {
            yield break;
        }
        processingRegeneration = true;
        string requestId = CoCreationDraftContext.RegenerationRequestId;
        bool succeeded = string.IsNullOrWhiteSpace(CoCreationDraftContext.RegenerationFailureCode);
        string action = succeeded ? "complete" : "fail";
        string endpoint = coCreationUrl.TrimEnd('/')
            + "/api/integrations/sessions/"
            + UnityWebRequest.EscapeURL(CoCreationDraftContext.SessionId)
            + "/draft-regenerations/"
            + UnityWebRequest.EscapeURL(requestId)
            + "/" + action;
        string json = succeeded
            ? JsonUtility.ToJson(new DraftRegenerationCompleteRequest { rows = CoCreationDraftContext.RegeneratedRows })
            : JsonUtility.ToJson(new DraftRegenerationFailRequest { failureCode = CoCreationDraftContext.RegenerationFailureCode });

        const int maxSubmitAttempts = 3;
        for (int attempt = 1; attempt <= maxSubmitAttempts; attempt++)
        {
            using (UnityWebRequest request = new UnityWebRequest(endpoint, "POST"))
            {
                request.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(json));
                request.downloadHandler = new DownloadHandlerBuffer();
                request.SetRequestHeader("Content-Type", "application/json");
                request.SetRequestHeader("Authorization", "Bearer " + CoCreationDraftContext.IntegrationToken);
                request.timeout = Mathf.Clamp(requestTimeoutSeconds, 1, 10);
                yield return request.SendWebRequest();

                if (request.result == UnityWebRequest.Result.Success)
                {
                    break;
                }

                if (request.responseCode >= 400 && request.responseCode < 500)
                {
                    NotifyBrowserRegenerationReturn(requestId, "failed");
                    CoCreationDraftContext.ClearRegeneration();
                    processingRegeneration = false;
                    SetButtonState(true, "RETURN TO CO-CREATION LAB");
                    SetStatus("The regeneration request expired or was cancelled. The previous draft was preserved.", ErrorStatusColor);
                    trackingRoutine = StartCoroutine(TrackSessionCompletion());
                    yield break;
                }

                if (attempt == maxSubmitAttempts)
                {
                    processingRegeneration = false;
                    SetButtonState(true, "RETRY DRAFT SYNC");
                    SetStatus(
                        "Draft result synchronization is unavailable. Select the button to retry; the generated result is preserved locally.",
                        ErrorStatusColor
                    );
                    yield break;
                }
            }

            SetStatus("Draft result synchronization failed. Retrying...", ErrorStatusColor);
            yield return new WaitForSecondsRealtime(2f);
        }

        if (succeeded)
        {
            CoCreationDraftContext.AcceptRegenerationResult();
        }
        NotifyBrowserRegenerationReturn(requestId, succeeded ? "completed" : "failed");
        CoCreationDraftContext.ClearRegeneration();
        processingRegeneration = false;
        SetButtonState(true, "RETURN TO CO-CREATION LAB");
        SetStatus(
            succeeded ? "The regenerated draft is ready in the lab." : "Regeneration failed; the previous draft was preserved.",
            succeeded ? ReadyStatusColor : ErrorStatusColor
        );
        trackingRoutine = StartCoroutine(TrackSessionCompletion());
    }

    private static void NotifyBrowserRegenerationReturn(string requestId, string status)
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanReturnDraftRegeneration(
            CoCreationDraftContext.SessionId,
            requestId ?? "",
            status ?? "failed"
        );
#endif
    }

    private void HandleCompletedSession(
        string[] finalRows,
        string designerIntention)
    {
        string normalizedDesignerIntention = (designerIntention ?? "").Trim();

        // A completed 8010 session is not ready to become an online challenge
        // until its final, designer-authored experience goal is present.  Do not
        // silently submit an empty value: Match_Result would then have no way to
        // distinguish a missing hand-off from an intentionally blank answer.
        if (string.IsNullOrWhiteSpace(normalizedDesignerIntention))
        {
            ApplyFailure(
                "The final design intention was not received. Return to the co-creation lab and submit it before continuing."
            );
            Debug.LogWarning(
                "CoCreationEntryController: Completed session did not include a designer intention."
            );
            return;
        }

        SetStatus(
            "Co-creation session complete. The confirmed Stage is synchronized.",
            ReadyStatusColor
        );
        SetButtonState(false, "SESSION COMPLETE");

        if (!OnlineMatchContext.HasMatch)
        {
            CoCreationDraftContext.Clear();
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

    private bool TryBuildResumeLabUrl(string sessionId, out string targetUrl)
    {
        targetUrl = "";

        if (!TryGetCoCreationUrl(out string baseUrl)
            || string.IsNullOrWhiteSpace(sessionId)
            || !Uri.TryCreate(baseUrl, UriKind.Absolute, out Uri baseUri))
        {
            return false;
        }

        UriBuilder builder = new UriBuilder(baseUri);
        builder.Fragment = "session=" + Uri.EscapeDataString(sessionId);
        targetUrl = builder.Uri.AbsoluteUri;
        return true;
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
    public string designerIntention;
    public DraftRegenerationIntegrationState draftRegeneration;
}

[Serializable]
public sealed class DraftRegenerationIntegrationState
{
    public string requestId;
    public string status;
    public int generation;
    public string requestedAt;
}

[Serializable]
public sealed class DraftRegenerationBridgeRequest
{
    public string sessionId;
    public string requestId;
}

[Serializable]
public sealed class DraftRegenerationCompleteRequest
{
    public string[] rows;
}

[Serializable]
public sealed class DraftRegenerationFailRequest
{
    public string failureCode;
}
