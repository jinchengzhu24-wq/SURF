using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Networking;

#if UNITY_EDITOR
using UnityEditor;
#endif

public class LLMLevelDesignClient : MonoBehaviour
{
    public string endpoint = "http://111.231.136.4:8000/generate-level-plan";
    public int requestTimeoutSeconds = 180;

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
            onSuccess?.Invoke(null);
            yield break;
        }

        isCancellingRequests = false;

        float startedAt = Time.realtimeSinceStartup;
        Debug.Log(
            "LLMLevelDesignClient request started:"
            + " endpoint=" + endpoint
            + ", timeoutSeconds=" + requestTimeoutSeconds
        );

        UnityWebRequest request = UnityWebRequest.Get(endpoint);
        request.timeout = Mathf.Max(1, requestTimeoutSeconds);
        request.SetRequestHeader("Accept", "application/json");
        activeRequests.Add(request);

        UnityWebRequestAsyncOperation operation = request.SendWebRequest();

        while (!operation.isDone)
        {
            if (isCancellingRequests || !isActiveAndEnabled)
            {
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

    private float GetElapsedSeconds(float startedAt)
    {
        return Mathf.Round((Time.realtimeSinceStartup - startedAt) * 100f) / 100f;
    }
}
