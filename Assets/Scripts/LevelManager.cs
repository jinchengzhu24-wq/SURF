using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class LevelManager : MonoBehaviour
{   
    public PlayerAnimation anim;

    [Header("Level Info")]
    public int boxCount;

    public int reachedCount;

    [Header("Black Panel")]
    public Image blackPanel;

    public float fadeTime = 1f;

    private void Start()
    {
        boxCount = FindObjectsOfType<Box>().Length;
        StartCoroutine(Fade(1, 0));
    }

    public void BoxReachTarget()
    {
        reachedCount++;

        if(reachedCount == boxCount)
        {
            anim.Win();
            StartCoroutine(LoadNextLevel());
        }
    }

    public void BoxLeaveTarget()
    {
        reachedCount--;
    }
    private IEnumerator LoadNextLevel()
    //协程:便于中间暂停的函数
    {
        yield return new WaitForSeconds(1.5f);

        yield return Fade(0, 1);

        int currentIndex = SceneManager.GetActiveScene().buildIndex;
        SceneManager.LoadScene(currentIndex + 1);
    }

    private IEnumerator Fade(float startAlpha, float endAlpha)
    {
        float timer = 0;

        while (timer < fadeTime)
        {
            timer += Time.deltaTime;

            Color color = blackPanel.color;
            color.a = Mathf.Lerp(startAlpha, endAlpha, timer / fadeTime);
            blackPanel.color = color;

            yield return null;
        }

        Color finalColor = blackPanel.color;
        finalColor.a = endAlpha;
        blackPanel.color = finalColor;
    }   
}
