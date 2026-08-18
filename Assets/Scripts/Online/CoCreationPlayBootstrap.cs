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

#if UNITY_WEBGL && !UNITY_EDITOR
    [DllImport("__Internal")]
    private static extern void SokobanClearCoCreationPlayQuery();
#endif

    private IEnumerator Start()
    {
        if (!TryReadQueryValue("cocreationAttempt", out string attemptId)
            || !TryReadQueryValue("cocreationPlay", out string ticket))
        {
            yield break;
        }

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
                yield break;
            }

            CoCreationPlayBootstrapResponse response;

            try
            {
                response = JsonUtility.FromJson<CoCreationPlayBootstrapResponse>(
                    request.downloadHandler.text
                );
                CoCreationPlayContext.Initialize(response);
            }
            catch (Exception exception)
            {
                Debug.LogError(
                    "CoCreationPlayBootstrap: Invalid Play bootstrap response. "
                    + exception.Message
                );
                yield break;
            }
        }

#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanClearCoCreationPlayQuery();
#endif

        string targetScene = CoCreationPlayContext.ResolveSceneName();

        if (!Application.CanStreamedLevelBeLoaded(targetScene))
        {
            Debug.LogError(
                "CoCreationPlayBootstrap: Play scene is unavailable: "
                + targetScene
            );
            CoCreationPlayContext.Clear();
            yield break;
        }

        SceneManager.LoadScene(targetScene);
    }

    private static bool TryReadQueryValue(string key, out string value)
    {
        value = "";
        string absoluteUrl = Application.absoluteURL;

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
}

[Serializable]
public sealed class CoCreationPlayBootstrapRequest
{
    public string ticket;
}
