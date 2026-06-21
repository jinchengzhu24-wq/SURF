using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class PlayerAnimation : MonoBehaviour
{   
    private Animator animator;

    private void Start()
    {
        animator = GetComponentInChildren<Animator>();
    }
    public void Win()
    {
        animator.SetTrigger("Win");
    }
}
