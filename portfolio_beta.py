# Importing the required modules
import yfinance as yf
import pandas as pd
import numpy as np

# Using the S&P 500 Market Ticker
marketTicker = "^GSPC"

# Creating a weighted portfolio
weightedPortfolio = {
    "AAPL": 0.6,
    "TSLA": 0.3,
    "RDDT": 0.1
}

# Getting returns of provided stocks
def getReturns(tickers):
    data = yf.download(tickers, period="2y", interval="1d")["Close"]
    returns = data.pct_change().dropna()
    return returns


# Defining a function to find the Beta of a stock
def stockBeta(assetRet, marketRet):
    merged = pd.concat([assetRet, marketRet], axis=1).dropna()
    assets = merged.iloc[:, 0]
    market = merged.iloc[:, 1]
    # Finding the Covariance matrix defining the delta degree of freedom = 1 (Sample variance)
    CovMatrix = np.cov(assets, market, ddof=1)
    Cov = CovMatrix[0, 1]
    Var = CovMatrix[1, 1]
    Beta = Cov / Var

    return Beta if Var != 0 else np.nan

tickers = list(weightedPortfolio.keys()) + [marketTicker]
returns = getReturns(tickers)

marketReturn = returns[marketTicker]

betas = {}

for i in tickers:
    betas[i] = stockBeta(returns[i], marketReturn)

portfolioBeta = sum(weightedPortfolio[t] * betas[t] for t in weightedPortfolio) 

print("Individual Betas:")
for i in weightedPortfolio:
    print(f" {i}: {betas[i]:.2f}")

print(f"The portfolio beta is {portfolioBeta:.2f}")