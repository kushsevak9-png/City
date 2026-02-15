import streamlit as st
try:
    st.feedback("stars")
    print("st.feedback is available")
except AttributeError:
    print("st.feedback is NOT available")
except Exception as e:
    print(f"Error: {e}")
