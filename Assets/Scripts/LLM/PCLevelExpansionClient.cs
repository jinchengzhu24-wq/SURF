using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

[Serializable]
public class PCLevelCandidateResponse
{
    public string[] rows;
}

public class PCLevelExpansionClient : MonoBehaviour
{
    public string endpoint = "http://111.231.136.4:8000/generate-pc-level";
    public int requestTimeoutSeconds = 180;

    public int LastAttemptsUsed { get; private set; }
    public bool LastFailureRetryable { get; private set; } = true;
    public string LastFailureMessage { get; private set; } = "";
    public string LastRequestId { get; private set; } = "";

    public IEnumerator RequestCandidate(
        PCDesignSketchData sketch,
        string[] previousCandidateRows,
        string rejectionReason,
        int maxAttempts,
        Action<PCLevelCandidateResponse> onComplete)
    {
        LastAttemptsUsed = 0;
        LastFailureRetryable = true;
        LastFailureMessage = "";
        LastRequestId = "";

        if (sketch == null || sketch.rows == null)
        {
            LastFailureRetryable = false;
            LastFailureMessage = "Saved PC design is missing.";
            onComplete?.Invoke(null);
            yield break;
        }

        int boundedMaxAttempts = 1;
        string requestId = LLMBackendError.CreateRequestId();
        string json;

        if (previousCandidateRows == null || previousCandidateRows.Length == 0)
        {
            PCLevelInitialGenerationRequest payload =
                new PCLevelInitialGenerationRequest
                {
                    width = sketch.width,
                    height = sketch.height,
                    sketchRows = CloneRows(sketch.rows),
                    rejectionReason = rejectionReason ?? "",
                    maxAttempts = boundedMaxAttempts
                };
            json = JsonUtility.ToJson(payload);
        }
        else
        {
            PCLevelGenerationRequest payload = new PCLevelGenerationRequest
            {
                width = sketch.width,
                height = sketch.height,
                sketchRows = CloneRows(sketch.rows),
                previousCandidateRows = CloneRows(previousCandidateRows),
                rejectionReason = rejectionReason ?? "",
                maxAttempts = boundedMaxAttempts
            };
            json = JsonUtility.ToJson(payload);
        }

        byte[] body = Encoding.UTF8.GetBytes(json);

        using (UnityWebRequest request = new UnityWebRequest(endpoint, "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(body);
            request.downloadHandler = new DownloadHandlerBuffer();
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            request.SetRequestHeader("Content-Type", "application/json");
            request.SetRequestHeader("Accept", "application/json");
            request.SetRequestHeader("X-Request-ID", requestId);

            yield return request.SendWebRequest();

            LastAttemptsUsed = Mathf.Max(1, LLMBackendError.GetAttemptsUsed(request));
            LastRequestId = LLMBackendError.GetRequestId(request, requestId);

            if (request.result != UnityWebRequest.Result.Success)
            {
                LastFailureRetryable = LLMBackendError.GetRetryable(request, true);
                LastFailureMessage = LLMBackendError.BuildDiagnostic(request, requestId);
                Debug.LogWarning(
                    "PCLevelExpansionClient request failed: "
                    + LastFailureMessage
                );
                onComplete?.Invoke(null);
                yield break;
            }

            PCLevelCandidateResponse response = null;

            try
            {
                response = JsonUtility.FromJson<PCLevelCandidateResponse>(
                    request.downloadHandler.text
                );
            }
            catch (Exception exception)
            {
                LastFailureMessage = "Could not parse PC level response: "
                    + exception.Message;
            }

            if (response == null || response.rows == null)
            {
                LastFailureMessage = string.IsNullOrEmpty(LastFailureMessage)
                    ? "PC level response did not contain rows."
                    : LastFailureMessage;
                onComplete?.Invoke(null);
                yield break;
            }

            LastFailureRetryable = false;
            onComplete?.Invoke(response);
        }
    }

    [Serializable]
    private class PCLevelInitialGenerationRequest
    {
        public int width;
        public int height;
        public string[] sketchRows;
        public string rejectionReason;
        public int maxAttempts;
    }

    [Serializable]
    private class PCLevelGenerationRequest
    {
        public int width;
        public int height;
        public string[] sketchRows;
        public string[] previousCandidateRows;
        public string rejectionReason;
        public int maxAttempts;
    }

    private static string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return null;
        }

        string[] clone = new string[rows.Length];

        for (int row = 0; row < rows.Length; row++)
        {
            clone[row] = rows[row] ?? "";
        }

        return clone;
    }
}
