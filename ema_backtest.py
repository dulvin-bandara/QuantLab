import yfinance as yf
import matplotlib.pyplot as plt

tickers = ["IVV"]

recent_data = yf.download("AAPL", period="5d")
print(recent_data)