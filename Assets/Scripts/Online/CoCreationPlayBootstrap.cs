using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;

#if UNITY_WEBGL && !UNITY_EDITOR
using System.Runtime.InteropServices;
#endif

[DefaultExecutionOrder(-500)]
public sealed class CoCreationPlayBootstrap : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "http://111.231.136.4/cocreation";

    [SerializeField]
    private string backendBaseUrl = DefaultBackendBaseUrl;

    [SerializeField]
    private int requestTimeoutSeconds = 15;

    private bool bootstrapping;

#if UNITY_WEBGL && !UNITY_EDITOR
    [DllImport("__Internal")]
    private static extern void SokobanClearCoCreationPlayQuery();

    [DllImport("__Internal")]
    private static extern void SokobanSetCoCreationPlayBridgeReady(int ready);

    [DllImport("__Internal")]
    private static extern void SokobanReturnToCoCreationLab(string status);
#endif

    private void Awake()
    {
        RefreshBrowserBridgeReady();
    }

    public static void RefreshBrowserBridgeReady()
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanSetCoCreationPlayBridgeReady(
            CoCreationDraftContext.HasDraft
            && !string.IsNullOrWhiteSpace(CoCreationDraftContext.SessionId)
                ? 1
                : 0
        );
#endif
    }

    private void OnDestroy()
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanSetCoCreationPlayBridgeReady(0);
#endif
    }

    private IEnumerator Start()
    {
        string launchUrl = Application.absoluteURL;
        if (!TryReadQueryValue(launchUrl, "cocreationAttempt", out _)
            || !TryReadQueryValue(launchUrl, "cocreationPlay", out _))
        {
            yield break;
        }

        yield return BootstrapAndLoad(launchUrl, "", false);
    }

    public void ReceiveBrowserPlayRequest(string messageJson)
    {
        if (bootstrapping)
        {
            return;
        }

        CoCreationBrowserPlayRequest message;

        try
        {
            message = JsonUtility.FromJson<CoCreationBrowserPlayRequest>(messageJson);
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                "CoCreationPlayBootstrap: Invalid browser Play message. "
                + exception.Message
            );
            NotifyEmbeddedFailure();
            return;
        }

        if (message == null
            || string.IsNullOrWhiteSpace(message.playUrl)
            || string.IsNullOrWhiteSpace(message.sessionId)
            || !string.Equals(
                message.sessionId,
                CoCreationDraftContext.SessionId,
                StringComparison.Ordinal))
        {
            Debug.LogWarning(
                "CoCreationPlayBootstrap: Browser Play message did not match the active session."
            );
            NotifyEmbeddedFailure();
            return;
        }

        StartCoroutine(
            BootstrapAndLoad(message.playUrl, message.sessionId, true)
        );
    }

    private IEnumerator BootstrapAndLoad(
        string launchUrl,
        string expectedSessionId,
        bool usesExistingUnityInstance)
    {
        if (bootstrapping)
        {
            yield break;
        }

        if (!TryReadQueryValue(launchUrl, "cocreationAttempt", out string attemptId)
            || !TryReadQueryValue(launchUrl, "cocreationPlay", out string ticket))
        {
            if (usesExistingUnityInstance)
            {
                NotifyEmbeddedFailure();
            }
            yield break;
        }

        bootstrapping = true;

        string endpoint = backendBaseUrl.TrimEnd('/')
            + "/api/play-attempts/"
            + UnityWebRequest.EscapeURL(attemptId)
            + "/bootstrap";
        string json = JsonUtility.ToJson(
            new CoCreationPlayBootstrapRequest { ticket = ticket }
        );

        using (UnityWebRequest request = new UnityWebRequest(endpoint, "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(json));
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
            {
                Debug.LogError(
                    "CoCreationPlayBootstrap: Play ticket exchange failed: "
                    + request.error
                    + " response="
                    + request.downloadHandler.text
                );
                bootstrapping = false;
                if (usesExistingUnityInstance)
                {
                    NotifyEmbeddedFailure();
                }
                yield break;
            }

            CoCreationPlayBootstrapResponse response;

            try
            {
                response = JsonUtility.FromJson<CoCreationPlayBootstrapResponse>(
                    request.downloadHandler.text
                );

                if (usesExistingUnityInstance
                    && !string.Equals(
                        response != null ? response.sessionId : "",
                        expectedSessionId,
                        StringComparison.Ordinal))
                {
                    throw new InvalidOperationException(
                        "The Play response belongs to another co-creation session."
                    );
                }

                CoCreationPlayContext.Initialize(
                    response,
                    usesExistingUnityInstance
                );
            }
            catch (Exception exception)
            {
                Debug.LogError(
                    "CoCreationPlayBootstrap: Invalid Play bootstrap response. "
                    + exception.Message
                );
                bootstrapping = false;
                if (usesExistingUnityInstance)
                {
                    NotifyEmbeddedFailure();
                }
                yield break;
            }
        }

#if UNITY_WEBGL && !UNITY_EDITOR
        if (!usesExistingUnityInstance)
        {
            SokobanClearCoCreationPlayQuery();
        }
#endif

        string targetScene = CoCreationPlayContext.ResolveSceneName();

        if (!Application.CanStreamedLevelBeLoaded(targetScene))
        {
            Debug.LogError(
                "CoCreationPlayBootstrap: Play scene is unavailable: "
                + targetScene
            );
            CoCreationPlayContext.Clear();
            bootstrapping = false;
            if (usesExistingUnityInstance)
            {
                NotifyEmbeddedFailure();
            }
            yield break;
        }

        SceneManager.LoadScene(targetScene);
    }

    private static bool TryReadQueryValue(
        string absoluteUrl,
        string key,
        out string value)
    {
        value = "";

        if (string.IsNullOrWhiteSpace(absoluteUrl)
            || !Uri.TryCreate(absoluteUrl, UriKind.Absolute, out Uri uri))
        {
            return false;
        }

        string query = uri.Query.TrimStart('?');

        foreach (string part in query.Split('&'))
        {
            if (string.IsNullOrEmpty(part))
            {
                continue;
            }

            string[] pair = part.Split(new[] { '=' }, 2);
            string candidateKey = Uri.UnescapeDataString(pair[0].Replace("+", " "));

            if (!string.Equals(candidateKey, key, StringComparison.Ordinal))
            {
                continue;
            }

            value = pair.Length > 1
                ? Uri.UnescapeDataString(pair[1].Replace("+", " "))
                : "";
            return !string.IsNullOrWhiteSpace(value);
        }

        return false;
    }

    private static void NotifyEmbeddedFailure()
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanReturnToCoCreationLab("load_failed");
#endif
    }
}

[Serializable]
public sealed class CoCreationPlayBootstrapRequest
{
    public string ticket;
}

[Serializable]
public sealed class CoCreationBrowserPlayRequest
{
    public string sessionId;
    public string playUrl;
}
