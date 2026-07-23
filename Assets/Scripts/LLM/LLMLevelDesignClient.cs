using System;
using System.Collections;
using System.Collections.Generic;
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

    private readonly List<UnityWebRequest> activeRequests = new List<UnityWebRequest>();
    private bool isCancellingRequests;

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

    public IEnumerator RequestPlan(Action<LevelDesignPlan> onSuccess)
    {
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
        string requestUrl = GetRequestUrl();
        string ideaText = GetCreativeIdeaText();

        float startedAt = Time.realtimeSinceStartup;
        Debug.Log(
            "LLMLevelDesignClient request started:"
            + " endpoint=" + requestUrl
            + ", timeoutSeconds=" + requestTimeoutSeconds
            + ", hasIdeaText=" + !string.IsNullOrEmpty(ideaText)
            + ", ideaTextLength=" + ideaText.Length
        );

        UnityWebRequest request = UnityWebRequest.Get(requestUrl);
        request.timeout = Mathf.Max(1, requestTimeoutSeconds);
        request.SetRequestHeader("Accept", "application/json");
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
            if (!isCancellingRequests)
            {
                Debug.LogWarning(
                    "LLMLevelDesignClient failed:"
                    + " error=" + request.error
                    + ", responseCode=" + request.responseCode
                    + ", elapsedSeconds=" + GetElapsedSeconds(startedAt)
                );
            }

            CleanupRequest(request);
            onSuccess?.Invoke(null);
            yield break;
        }

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
            ApplyLatestAdjustmentConstraints(plan);
            Debug.Log(
                "LLMLevelDesignClient received plan:"
                + " responseCode=" + request.responseCode
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

    private string GetRequestUrl()
    {
        string requestUrl = endpoint;
        string ideaText = GetCreativeIdeaText();

        if (!includeCreativeWorkshopIdea || string.IsNullOrEmpty(ideaText))
        {
            return requestUrl;
        }

        requestUrl = AppendQueryParameter(requestUrl, "ideaText", ideaText);
        requestUrl = AppendQueryParameter(requestUrl, "ideaId", PlayerPrefs.GetString(CreativeIdeaIdPrefsKey, ""));
        requestUrl = AppendQueryParameter(requestUrl, "sessionId", PlayerPrefs.GetString(CreativeIdeaSessionIdPrefsKey, ""));
        requestUrl = AppendQueryParameter(requestUrl, "sceneName", SceneManager.GetActiveScene().name);
        requestUrl = AppendStructuredContext(requestUrl, "originalIdeaText", CreativeWorkshopContext.OriginalIdeaTextPrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "selectedDirectionText", CreativeWorkshopContext.SelectedDirectionTextPrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "refinementFeedbackText", CreativeWorkshopContext.RefinementFeedbackTextPrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "adjustmentHistoryText", CreativeWorkshopContext.AdjustmentHistoryTextPrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "latestAdjustmentText", CreativeWorkshopContext.LatestAdjustmentTextPrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "revisionMode", CreativeWorkshopContext.RevisionModePrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "previousLevelPlan", CreativeWorkshopContext.PreviousLevelPlanPrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "previousLevelMetrics", CreativeWorkshopContext.PreviousLevelMetricsPrefsKey);
        requestUrl = AppendStructuredContext(requestUrl, "selectedHAPlan", CreativeWorkshopContext.SelectedHAPlanPrefsKey);
        return requestUrl;
    }

    private string AppendStructuredContext(string url, string parameterName, string prefsKey)
    {
        return AppendQueryParameter(url, parameterName, PlayerPrefs.GetString(prefsKey, ""));
    }

    private string GetCreativeIdeaText()
    {
        return !string.IsNullOrEmpty(CreativeWorkshopContext.IdeaText)
            ? CreativeWorkshopContext.IdeaText
            : PlayerPrefs.GetString(CreativeIdeaTextPrefsKey, "");
    }

    private string AppendQueryParameter(string url, string key, string value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return url;
        }

        string separator = url.Contains("?") ? "&" : "?";
        return url + separator + key + "=" + Uri.EscapeDataString(value);
    }

    private float GetElapsedSeconds(float startedAt)
    {
        return Mathf.Round((Time.realtimeSinceStartup - startedAt) * 100f) / 100f;
    }
}
