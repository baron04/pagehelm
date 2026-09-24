"""Goal-directed turns: one model call declares the goal, the server resolves and acts.

The classic loop asks the model to choose, step by step, among every tool the session
offers and then checks its claims afterwards. A goal-directed turn inverts that: the model
is asked once what the person wants, in the vocabulary of the application's own index,
and everything after that -- finding the record, reading the fields, opening the page,
selecting the tab, highlighting the control, checking the outcome, writing the reply -- is
deterministic server code the adapter and the runtime already know how to do.
"""
