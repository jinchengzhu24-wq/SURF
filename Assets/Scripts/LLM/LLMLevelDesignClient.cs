using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;

#if UNITY_EDITOR
using UnityEditor;
#endif

public class LLMLevelDesignClient : MonoBehaviour
{
    private const string CreativeIdeaIdPrefsKey = "SokobanCreativeWorkshopIdeaId";
    private const string CreativeIdeaSessionIdPrefsKey = "SokobanCreativeWorkshopIdeaSessionId";
    private const string CreativeIdeaTextPrefsKey = "SokobanCreativeWorkshopIdeaText";

    public string endpoint = "http://111.231.136.4:8000/generate-level-plan";
    public int requestTimeoutSeconds = 180;
    public bool includeCreativeWorkshopIdea;
    [Tooltip(
        "Enable this on a dedicated Description-to-Level result scene to "
        + "consume the settings saved by the DG scene."
    )]
    public bool includeDescriptionGenerationPreferences;

    private readonly List<UnityWebRequest> activeRequests = new List<UnityWebRequest>();
    private bool isCancellingRequests;

    public int LastAttemptsUsed { get; private set; }
    public string LastRequestId { get; private set; }
    public bool LastFailureRetryable { get; private set; }

#if UNITY_EDITOR
    [InitializeOnLoadMethod]
    private static void AllowHttpRequestsInEditor()
    {
        if (PlayerSettings.insecureHttpOption != InsecureHttpOption.AlwaysAllowed)
        {
            PlayerSettings.insecureHttpOption = InsecureHttpOption.AlwaysAllowed;
        }
    }

    [InitializeOnLoadMethod]
    private static void RegisterAssemblyReloadHook()
    {
        AssemblyReloadEvents.beforeAssemblyReload -= CancelAllActiveClients;
        AssemblyReloadEvents.beforeAssemblyReload += CancelAllActiveClients;
    }

    private static void CancelAllActiveClients()
    {
        LLMLevelDesignClient[] clients = Resources.FindObjectsOfTypeAll<LLMLevelDesignClient>();

        for (int i = 0; i < clients.Length; i++)
        {
            clients[i].CancelActiveRequests();
        }
    }
#endif

    private void OnEnable()
    {
        isCancellingRequests = false;
    }

    private void OnDisable()
    {
        CancelActiveRequests();
    }

    private void OnDestroy()
    {
        CancelActiveRequests();
    }

    public void CancelActiveRequests()
    {
        isCancellingRequests = true;

        for (int i = activeRequests.Count - 1; i >= 0; i--)
        {
            UnityWebRequest request = activeRequests[i];

            if (request == null)
            {
                continue;
            }

            request.Abort();
            request.Dispose();
            activeRequests.RemoveAt(i);
        }
    }

    public IEnumerator RequestPlan(
        Action<LevelDesignPlan> onSuccess,
        int maxAttempts = 2)
    {
        LastAttemptsUsed = 0;
        LastRequestId = "";
        LastFailureRetryable = true;

        if (!isActiveAndEnabled)
        {
            Debug.LogWarning(
                "LLMLevelDesignClient skipped request because client is inactive:"
                + " object=" + gameObject.name
                + ", activeSelf=" + gameObject.activeSelf
                + ", activeInHierarchy=" + gameObject.activeInHierarchy
                + ", enabled=" + enabled
            );
            onSuccess?.Invoke(null);
            yield break;
        }

        isCancellingRequests = false;
        int boundedMaxAttempts = Mathf.Clamp(maxAttempts, 1, 2);
        string ideaText = GetCreativeIdeaText();
        string requestId = LLMBackendError.CreateRequestId();
        string json = JsonUtility.ToJson(BuildRequestPayload(boundedMaxAttempts));
        byte[] body = Encoding.UTF8.GetBytes(json);

        float startedAt = Time.realtimeSinceStartup;
        Debug.Log(
            "LLMLevelDesignClient request started:"
            + " endpoint=" + endpoint
            + ", timeoutSeconds=" + requestTimeoutSeconds
            + ", maxAttempts=" + boundedMaxAttempts
            + ", requestId=" + requestId
            + ", hasIdeaText=" + !string.IsNullOrEmpty(ideaText)
            + ", ideaTextLength=" + ideaText.Length
        );

        UnityWebRequest request = new UnityWebRequest(endpoint, "POST");
        request.uploadHandler = new UploadHandlerRaw(body);
        request.downloadHandler = new DownloadHandlerBuffer();
        request.timeout = Mathf.Max(1, requestTimeoutSeconds);
        request.SetRequestHeader("Content-Type", "application/json");
        request.SetRequestHeader("Accept", "application/json");
        request.SetRequestHeader("X-Request-ID", requestId);
        activeRequests.Add(request);

        UnityWebRequestAsyncOperation operation = request.SendWebRequest();

        while (!operation.isDone)
        {
            if (isCancellingRequests || !isActiveAndEnabled)
            {
                Debug.LogWarning(
                    "LLMLevelDesignClient request aborted:"
                    + " isCancellingRequests=" + isCancellingRequests
                    + ", activeSelf=" + gameObject.activeSelf
                    + ", activeInHierarchy=" + gameObject.activeInHierarchy
                    + ", enabled=" + enabled
                    + ", elapsedSeconds=" + GetElapsedSeconds(startedAt)
                );
                request.Abort();
                CleanupRequest(request);
                onSuccess?.Invoke(null);
                yield break;
            }

            yield return null;
        }

        if (request.result != UnityWebRequest.Result.Success)
        {
            LastAttemptsUsed = LLMBackendError.GetAttemptsUsed(request);
            LastRequestId = LLMBackendError.GetRequestId(request, requestId);
            LastFailureRetryable = LLMBackendError.GetRetryable(request, true);

            if (!isCancellingRequests)
            {
                Debug.LogWarning(
                    "LLMLevelDesignClient failed:"
                    + " " + LLMBackendError.BuildDiagnostic(request, requestId)
                    + ", elapsedSeconds=" + GetElapsedSeconds(startedAt)
                );
            }

            CleanupRequest(request);
            onSuccess?.Invoke(null);
            yield break;
        }

        LastAttemptsUsed = Mathf.Max(1, LLMBackendError.GetAttemptsUsed(request));
        LastRequestId = LLMBackendError.GetRequestId(request, requestId);
        LevelDesignPlan plan = null;

        try
        {
            plan = JsonUtility.FromJson<LevelDesignPlan>(request.downloadHandler.text);
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                "LLMLevelDesignClient could not parse plan JSON:"
                + " error=" + exception.Message
                + ", responseCode=" + request.responseCode
                + ", elapsedSeconds=" + GetElapsedSeconds(startedAt)
            );
        }

        if (plan != null)
        {
            LastFailureRetryable = false;
            ApplyLatestAdjustmentConstraints(plan);
            Debug.Log(
                "LLMLevelDesignClient received plan:"
                + " responseCode=" + request.responseCode
                + ", requestId=" + LastRequestId
                + ", attemptsUsed=" + LastAttemptsUsed
                + ", elapsedSeconds=" + GetElapsedSeconds(startedAt)
                + ", solutionSteps=" + plan.minSolutionSteps + "-" + plan.maxSolutionSteps
                + ", pushes=" + plan.minPushes + "-" + plan.maxPushes
                + ", waterAreas=" + plan.minWaterAreas + "-" + plan.maxWaterAreas
                + ", wallObstacleBlocks=" + plan.minWallObstacleBlocks + "-" + plan.maxWallObstacleBlocks
                + ", reversePulls=" + plan.minReversePulls + "-" + plan.maxReversePulls
                + ", archetype=" + plan.archetype
                + ", targetLayout=" + plan.targetLayout
                + ", obstacleStyle=" + plan.obstacleStyle
                + ", waterStyle=" + plan.waterStyle
                + ", style=" + plan.style
            );
        }

        CleanupRequest(request);
        onSuccess?.Invoke(plan);
    }

    private void ApplyLatestAdjustmentConstraints(LevelDesignPlan plan)
    {
        if (plan == null)
        {
            return;
        }

        string revisionMode = PlayerPrefs.GetString(
            CreativeWorkshopContext.RevisionModePrefsKey,
            ""
        );

        if (string.Equals(revisionMode, "ai", StringComparison.OrdinalIgnoreCase)
            || string.Equals(revisionMode, "ha", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        string adjustment = PlayerPrefs.GetString(
            CreativeWorkshopContext.LatestAdjustmentTextPrefsKey,
            ""
        ).ToLowerInvariant();
        bool narrow = ContainsAny(
            adjustment,
            "narrow corridor",
            "narrow passage",
            "one-tile",
            "single-tile",
            "窄道",
            "狭窄通道",
            "单格通道",
            "瓶颈"
        );
        bool center = ContainsAny(adjustment, "center", "central", "middle", "中心", "中央", "中间");

        if (!narrow || !center)
        {
            return;
        }

        bool requiresBoxRoute = ContainsAny(adjustment, "box", "crate", "箱子")
            && ContainsAny(adjustment, "must", "required", "pass through", "cross", "必须", "经过", "穿过");
        bool horizontal = ContainsAny(adjustment, "horizontal", "横向", "水平");
        bool vertical = ContainsAny(adjustment, "vertical", "纵向", "垂直");

        plan.archetype = "bottleneck_corridor";
        plan.obstacleStyle = "central_baffle";
        plan.corridorPlacement = "center";
        plan.corridorWidth = ContainsAny(adjustment, "two-tile", "two tile", "2-tile", "两格", "二格") ? 2 : 1;
        plan.corridorOrientation = horizontal == vertical
            ? "any"
            : horizontal ? "horizontal" : "vertical";
        plan.corridorRole = requiresBoxRoute ? "required_box_route" : "player_route";
        plan.corridorPriority = "required";
    }

    private bool ContainsAny(string value, params string[] tokens)
    {
        for (int i = 0; i < tokens.Length; i++)
        {
            if (value.Contains(tokens[i]))
            {
                return true;
            }
        }

        return false;
    }

    private void CleanupRequest(UnityWebRequest request)
    {
        if (request == null)
        {
            return;
        }

        if (activeRequests.Remove(request))
        {
            request.Dispose();
        }
    }

    private LevelPlanRequest BuildRequestPayload(int maxAttempts)
    {
        string ideaText = GetCreativeIdeaText();
        bool hasContext = includeCreativeWorkshopIdea && !string.IsNullOrEmpty(ideaText);
        DescriptionGenerationSettings descriptionSettings = null;
        bool hasDescriptionSettings =
            includeDescriptionGenerationPreferences
            && DescriptionGenerationContext.TryLoad(
                out descriptionSettings
            );

        return new LevelPlanRequest
        {
            ideaText = hasContext ? ideaText : "",
            ideaId = hasContext
                ? PlayerPrefs.GetString(CreativeIdeaIdPrefsKey, "")
                : "",
            sessionId = hasContext
                ? PlayerPrefs.GetString(CreativeIdeaSessionIdPrefsKey, "")
                : "",
            sceneName = hasContext ? SceneManager.GetActiveScene().name : "",
            competitionMode = SceneManager.GetActiveScene().name == "DG_Level"
                ? CompetitionModeController.GetSelectedMode()
                : "",
            originalIdeaText = GetContextValue(
                hasContext,
                CreativeWorkshopContext.OriginalIdeaTextPrefsKey
            ),
            selectedDirectionText = GetContextValue(
                hasContext,
                CreativeWorkshopContext.SelectedDirectionTextPrefsKey
            ),
            refinementFeedbackText = GetContextValue(
                hasContext,
                CreativeWorkshopContext.RefinementFeedbackTextPrefsKey
            ),
            adjustmentHistoryText = GetContextValue(
                hasContext,
                CreativeWorkshopContext.AdjustmentHistoryTextPrefsKey
            ),
            latestAdjustmentText = GetContextValue(
                hasContext,
                CreativeWorkshopContext.LatestAdjustmentTextPrefsKey
            ),
            revisionMode = GetContextValue(
                hasContext,
                CreativeWorkshopContext.RevisionModePrefsKey
            ),
            previousLevelPlan = GetContextValue(
                hasContext,
                CreativeWorkshopContext.PreviousLevelPlanPrefsKey
            ),
            previousLevelMetrics = GetContextValue(
                hasContext,
                CreativeWorkshopContext.PreviousLevelMetricsPrefsKey
            ),
            selectedHAPlan = GetContextValue(
                hasContext,
                CreativeWorkshopContext.SelectedHAPlanPrefsKey
            ),
            styleDescription = hasDescriptionSettings
                ? descriptionSettings.styleDescription
                : "",
            generationPreferences = hasDescriptionSettings
                ? descriptionSettings.preferences
                : null,
            maxAttempts = maxAttempts
        };
    }

    private string GetCreativeIdeaText()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaText)
            ? CreativeWorkshopContext.IdeaText
            : PlayerPrefs.GetString(CreativeIdeaTextPrefsKey, "");
    }

    private string GetContextValue(bool hasContext, string prefsKey)
    {
        return hasContext ? PlayerPrefs.GetString(prefsKey, "") : "";
    }

    private float GetElapsedSeconds(float startedAt)
    {
        return Mathf.Round((Time.realtimeSinceStartup - startedAt) * 100f) / 100f;
    }

    [Serializable]
    private class LevelPlanRequest
    {
        public string ideaText;
        public string ideaId;
        public string sessionId;
        public string sceneName;
        public string competitionMode;
        public string originalIdeaText;
        public string selectedDirectionText;
        public string refinementFeedbackText;
        public string adjustmentHistoryText;
        public string latestAdjustmentText;
        public string revisionMode;
        public string previousLevelPlan;
        public string previousLevelMetrics;
        public string selectedHAPlan;
        public string styleDescription;
        public LevelGenerationPreferences generationPreferences;
        public int maxAttempts;
    }
}

public static class LLMBackendError
{
    [Serializable]
    private sealed class PlainErrorEnvelope
    {
        public string detail = "";
    }

    [Serializable]
    private sealed class ErrorEnvelope
    {
        public ErrorDetail detail = null;
    }

    [Serializable]
    private sealed class ErrorDetail
    {
        public string code = "";
        public string stage = "";
        public string message = "";
        public string requestId = "";
        public bool retryable = false;
        public int attemptsUsed = 0;
    }

    public static string CreateRequestId()
    {
        return Guid.NewGuid().ToString("N");
    }

    public static int GetAttemptsUsed(UnityWebRequest request)
    {
        if (request == null)
        {
            return 0;
        }

        string value = request.GetResponseHeader("X-LLM-Attempts-Used");
        return int.TryParse(value, out int attempts) ? Mathf.Max(0, attempts) : 0;
    }

    public static string GetRequestId(
        UnityWebRequest request,
        string fallbackRequestId = ""
    )
    {
        if (request == null)
        {
            return fallbackRequestId ?? string.Empty;
        }

        string responseRequestId = request.GetResponseHeader("X-Request-ID");
        return string.IsNullOrWhiteSpace(responseRequestId)
            ? fallbackRequestId ?? string.Empty
            : responseRequestId;
    }

    public static string BuildDiagnostic(
        UnityWebRequest request,
        string fallbackRequestId = ""
    )
    {
        string requestId = GetRequestId(request, fallbackRequestId);
        int attemptsUsed = GetAttemptsUsed(request);
        long responseCode = request != null ? request.responseCode : 0;
        string transportError = request != null ? request.error : string.Empty;
        string responseBody = request != null && request.downloadHandler != null
            ? request.downloadHandler.text
            : string.Empty;

        ErrorDetail detail = ParseDetail(responseBody);

        if (detail != null)
        {
            if (!string.IsNullOrWhiteSpace(detail.requestId))
            {
                requestId = detail.requestId;
            }

            if (detail.attemptsUsed > 0)
            {
                attemptsUsed = detail.attemptsUsed;
            }

            return $"error={transportError}, responseCode={responseCode}, code={detail.code}, " +
                   $"stage={detail.stage}, requestId={requestId}, attemptsUsed={attemptsUsed}, " +
                   $"retryable={detail.retryable}, detail={detail.message}";
        }

        return $"error={transportError}, responseCode={responseCode}, code=UNSTRUCTURED_ERROR, " +
               $"stage=http_response, requestId={requestId}, attemptsUsed={attemptsUsed}";
    }

    public static bool GetRetryable(
        UnityWebRequest request,
        bool fallback = true
    )
    {
        string responseBody = request != null && request.downloadHandler != null
            ? request.downloadHandler.text
            : string.Empty;
        ErrorDetail detail = ParseDetail(responseBody);
        return detail != null ? detail.retryable : fallback;
    }

    private static ErrorDetail ParseDetail(string responseBody)
    {
        if (string.IsNullOrWhiteSpace(responseBody))
        {
            return null;
        }

        try
        {
            PlainErrorEnvelope plainEnvelope =
                JsonUtility.FromJson<PlainErrorEnvelope>(responseBody);

            if (plainEnvelope != null
                && !string.IsNullOrWhiteSpace(plainEnvelope.detail))
            {
                return new ErrorDetail
                {
                    code = "REQUEST_VALIDATION_ERROR",
                    stage = "request_validation",
                    message = plainEnvelope.detail,
                    retryable = false
                };
            }
        }
        catch (Exception)
        {
            // Structured LLM errors store detail as an object, so parsing that
            // response with the plain-string envelope can legitimately fail.
        }

        try
        {
            ErrorEnvelope envelope = JsonUtility.FromJson<ErrorEnvelope>(responseBody);
            return envelope != null ? envelope.detail : null;
        }
        catch (Exception)
        {
            return null;
        }
    }
}
